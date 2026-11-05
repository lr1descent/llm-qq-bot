import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.mentions import MEMBER_ID
from app.schemas import ModelRule, Settings


def now():
    return datetime.now(UTC).isoformat()


class Conflict(ValueError):
    pass


class ModelSelectionError(ValueError):
    pass


DEFAULT_SETTINGS = Settings(
    revision=1,
    persona="你是群聊中的一位友善、机智且有分寸的伙伴。结合最近的聊天回应当前提问，注意区分不同发言者。默认用简洁自然的中文回答；不确定时直接说明。不要编造未提供的群聊历史。",
    models=[],
    default_model="",
    context_messages=30,
    max_output_tokens=4096,
)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS admin (
                    id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL,
                    external_id TEXT, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    context_after INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(source, external_id));
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    external_id TEXT, role TEXT NOT NULL, sender TEXT NOT NULL,
                    sender_id TEXT NOT NULL, content TEXT NOT NULL,
                    mentioned INTEGER NOT NULL, created_at TEXT NOT NULL,
                    delivery TEXT NOT NULL,
                    UNIQUE(conversation_id, external_id));
                CREATE INDEX IF NOT EXISTS messages_conversation
                    ON messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS message_members (
                    message_id INTEGER NOT NULL REFERENCES messages(id),
                    member_id TEXT NOT NULL, name TEXT NOT NULL,
                    PRIMARY KEY(message_id, member_id));
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    trigger_id INTEGER NOT NULL UNIQUE REFERENCES messages(id),
                    reply_id INTEGER REFERENCES messages(id),
                    status TEXT NOT NULL, model TEXT NOT NULL,
                    settings_json TEXT NOT NULL, context_json TEXT,
                    error TEXT, response_id TEXT, input_tokens INTEGER,
                    output_tokens INTEGER, latency_ms INTEGER,
                    created_at TEXT NOT NULL, finished_at TEXT);
                CREATE INDEX IF NOT EXISTS runs_queue ON runs(status, created_at);
            """)
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES (1, ?)",
                (DEFAULT_SETTINGS.model_dump_json(),),
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            return Settings.model_validate_json(
                db.execute("SELECT value FROM settings").fetchone()[0]
            )

    def update_settings(self, settings: Settings):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(db.execute("SELECT value FROM settings").fetchone()[0])
            if current["revision"] != settings.revision:
                raise Conflict("配置已被其他页面修改，请刷新后重新编辑")
            updated = settings.model_copy(update={"revision": settings.revision + 1})
            db.execute("UPDATE settings SET value=? WHERE id=1", (updated.model_dump_json(),))
        return updated

    def sync_models(self, model_ids: list[str]):
        """Replace the discovered catalog without overwriting administrator preferences."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = Settings.model_validate_json(
                db.execute("SELECT value FROM settings").fetchone()[0]
            )
            enabled = {model.id: model.enabled for model in current.models}
            models = [ModelRule(id=id_, enabled=enabled.get(id_, True)) for id_ in model_ids]
            default = current.default_model
            if default not in {model.id for model in models if model.enabled}:
                default = ""
            if current.models == models and current.default_model == default:
                return current
            updated = current.model_copy(
                update={
                    "models": models,
                    "default_model": default,
                    "revision": current.revision + 1,
                }
            )
            db.execute("UPDATE settings SET value=? WHERE id=1", (updated.model_dump_json(),))
            return updated

    def create_conversation(self, title, source="console", external_id=None):
        conversation_id = uuid4().hex
        timestamp = now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO conversations(id, source, external_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, source, external_id, title, timestamp, timestamp),
            )
        return self.conversation(conversation_id)

    def conversation(self, conversation_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise KeyError("会话不存在")
            return dict(row)

    def list_conversations(self, query, source, offset, limit):
        # LIKE metacharacters are escaped so user searches remain literal.
        term = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = """WHERE (?='' OR c.source=?) AND (c.title LIKE ? ESCAPE '\\'
                   OR c.external_id LIKE ? ESCAPE '\\' OR EXISTS
                   (SELECT 1 FROM messages m WHERE m.conversation_id=c.id
                    AND m.content LIKE ? ESCAPE '\\'))"""
        params = (source, source, term, term, term)
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM conversations c " + where, params).fetchone()[
                0
            ]
            rows = db.execute(
                """SELECT c.*,
                (SELECT COUNT(*) FROM messages WHERE conversation_id=c.id) AS message_count,
                (SELECT content FROM messages WHERE conversation_id=c.id ORDER BY id DESC LIMIT 1) AS preview,
                (SELECT model FROM runs WHERE conversation_id=c.id ORDER BY created_at DESC LIMIT 1) AS model
                FROM conversations c """
                + where
                + " ORDER BY c.updated_at DESC, c.id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total}

    def detail(self, conversation_id, before, limit):
        conversation = self.conversation(conversation_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND (? IS NULL OR id<?) "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, before, before, limit + 1),
            ).fetchall()
            messages = [dict(row) for row in reversed(rows[:limit])]
            first_id = messages[0]["id"] if messages else 0
            last_id = messages[-1]["id"] if messages else 0
            runs = db.execute(
                "SELECT id, trigger_id, reply_id, status, model, error, input_tokens, output_tokens, "
                "latency_ms, created_at FROM runs WHERE conversation_id=? "
                "AND (trigger_id BETWEEN ? AND ? OR reply_id BETWEEN ? AND ? "
                "OR status IN ('queued','running')) ORDER BY created_at DESC",
                (conversation_id, first_id, last_id, first_id, last_id),
            ).fetchall()
        return {
            "conversation": conversation,
            "messages": messages,
            "has_more": len(rows) > limit,
            "runs": [dict(row) for row in runs],
        }

    def ingest(
        self,
        *,
        conversation_id,
        content,
        sender,
        sender_id,
        mentioned,
        external_id=None,
        timestamp=None,
        model=None,
        mention_targets=(),
    ):
        for member in mention_targets:
            if not MEMBER_ID.fullmatch(member["id"]) or member["id"] == "all":
                raise ValueError("无效的群成员 ID")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = Settings.model_validate_json(
                db.execute("SELECT value FROM settings").fetchone()[0]
            )
            selected = model or settings.default_model
            if mentioned and not selected:
                raise ModelSelectionError("尚未选择默认模型，请在模型路由中同步上游并选择模型。")
            if mentioned and selected not in {rule.id for rule in settings.models if rule.enabled}:
                raise ModelSelectionError("该模型未被允许，无法发起调用")
            existing = (
                db.execute(
                    "SELECT id FROM messages WHERE conversation_id=? AND external_id=?",
                    (conversation_id, external_id),
                ).fetchone()
                if external_id
                else None
            )
            if existing:
                return {"duplicate": True, "message_id": existing[0], "run_id": None}
            message_id = db.execute(
                "INSERT INTO messages(conversation_id, external_id, role, sender, sender_id, "
                "content, mentioned, created_at, delivery) VALUES (?, ?, 'user', ?, ?, ?, ?, ?, 'received')",
                (
                    conversation_id,
                    external_id,
                    sender,
                    sender_id,
                    content,
                    mentioned,
                    timestamp or now(),
                ),
            ).lastrowid
            db.executemany(
                "INSERT OR IGNORE INTO message_members VALUES (?, ?, ?)",
                [(message_id, member["id"], member["name"]) for member in mention_targets],
            )
            run_id = uuid4().hex if mentioned else None
            if run_id:
                db.execute(
                    "INSERT INTO runs(id, conversation_id, trigger_id, status, model, settings_json, created_at) "
                    "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                    (
                        run_id,
                        conversation_id,
                        message_id,
                        selected,
                        settings.model_dump_json(),
                        now(),
                    ),
                )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), conversation_id))
        return {"duplicate": False, "message_id": message_id, "run_id": run_id}

    def qq_conversation(self, group_id, *, title=None):
        with self.connect() as db:
            timestamp = now()
            db.execute(
                "INSERT OR IGNORE INTO conversations(id, source, external_id, title, created_at, updated_at) "
                "VALUES (?, 'qq', ?, ?, ?, ?)",
                (uuid4().hex, group_id, title or "QQ群 · " + group_id, timestamp, timestamp),
            )
            return db.execute(
                "SELECT id FROM conversations WHERE source='qq' AND external_id=?",
                (group_id,),
            ).fetchone()[0]

    def reset_context(self, conversation_id):
        self.conversation(conversation_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT 1 FROM runs WHERE conversation_id=? AND status IN ('queued','running')",
                (conversation_id,),
            ).fetchone()
            if pending:
                raise Conflict("会话仍有待处理请求，请完成后再清空上下文")
            db.execute(
                "UPDATE conversations SET context_after=COALESCE((SELECT MAX(id) FROM messages "
                "WHERE conversation_id=?),0) WHERE id=?",
                (conversation_id, conversation_id),
            )

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("调用记录不存在")
        result = dict(row)
        result["settings"] = json.loads(result.pop("settings_json"))
        result["context"] = json.loads(result.pop("context_json") or "[]")
        return result

    def recover(self):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status='failed', error=?, finished_at=? WHERE status='running'",
                ("服务在处理期间停止；为避免重复回复，没有自动重试", now()),
            )

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE runs SET status='running' WHERE id=?", (row[0],))
        return self.run(row[0])

    def context(self, run):
        conversation = self.conversation(run["conversation_id"])
        is_onebot = conversation["source"] == "qq" and conversation["external_id"].startswith(
            "onebot:"
        )
        with self.connect() as db:
            # Exclude messages received after the triggering question, but include replies to
            # earlier questions that completed while this question was waiting in the queue.
            rows = db.execute(
                """
                SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id
                LEFT JOIN runs r ON r.reply_id=m.id
                WHERE m.conversation_id=? AND m.id>c.context_after
                AND (m.id<=? OR (m.role='assistant' AND r.trigger_id<?))
                AND m.delivery IN ('received','sent','local')
                ORDER BY COALESCE(r.trigger_id,m.id) DESC, m.id DESC LIMIT ?
                """,
                (
                    run["conversation_id"],
                    run["trigger_id"],
                    run["trigger_id"],
                    run["settings"]["context_messages"],
                ),
            ).fetchall()
            context = [dict(row) for row in reversed(rows)]
            for message in context:
                message["mention_targets"] = [
                    dict(member)
                    for member in db.execute(
                        "SELECT member_id AS id, name FROM message_members WHERE message_id=?",
                        (message["id"],),
                    )
                ]
                # OneBot sender_id is already the authenticated event's numeric QQ ID.
                # Derive it here so older stored messages work without a duplicated roster.
                if (
                    is_onebot
                    and message["role"] == "user"
                    and re.fullmatch(r"[0-9]{1,128}", message["sender_id"])
                ):
                    message["mention_targets"].append(
                        {"id": message["sender_id"], "name": message["sender"]}
                    )
        return context

    def save_context(self, run_id, context):
        with self.connect() as db:
            db.execute("UPDATE runs SET context_json=? WHERE id=?", (json.dumps(context), run_id))

    def save_reply(self, run, result, latency_ms, *, mentions=()):
        with self.connect() as db:
            reply_id = db.execute(
                "INSERT INTO messages(conversation_id, role, sender, sender_id, content, mentioned, "
                "created_at, delivery) VALUES (?, 'assistant', '机器人', 'bot', ?, 0, ?, 'pending')",
                (run["conversation_id"], result.text, now()),
            ).lastrowid
            db.executemany(
                "INSERT OR IGNORE INTO message_members VALUES (?, ?, ?)",
                [(reply_id, member.id, member.name) for member in mentions],
            )
            db.execute(
                "UPDATE runs SET reply_id=?, response_id=?, input_tokens=?, output_tokens=?, latency_ms=? WHERE id=?",
                (
                    reply_id,
                    result.response_id,
                    result.input_tokens,
                    result.output_tokens,
                    latency_ms,
                    run["id"],
                ),
            )
        return reply_id

    def finish(self, run_id, status, error=None, delivery=None):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?, error=?, finished_at=? WHERE id=?",
                (status, error, now(), run_id),
            )
            if delivery:
                db.execute(
                    "UPDATE messages SET delivery=? WHERE id=(SELECT reply_id FROM runs WHERE id=?)",
                    (delivery, run_id),
                )
            db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=(SELECT conversation_id FROM runs WHERE id=?)",
                (now(), run_id),
            )
