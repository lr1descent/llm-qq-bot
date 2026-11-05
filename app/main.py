import asyncio
import hashlib
import hmac
import json
import os
import re
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import Auth
from app.catalog import fetch_catalog
from app.onebot import GroupMessage, OneBotSender
from app.providers import (
    DEFAULT_OPENAI_BASE_URL,
    OpenAIProvider,
    ProviderError,
    QQProvider,
    signing_key,
)
from app.schemas import Credentials, NewConversation, NewMessage, QQEvent, Settings
from app.service import BotService
from app.store import Conflict, ModelSelectionError, Store

ROOT = Path(__file__).resolve().parent.parent
MAX_BODY_BYTES = 262144


@dataclass(frozen=True)
class Config:
    db_path: Path
    api_key: str = ""
    api_base_url: str = DEFAULT_OPENAI_BASE_URL
    qq_transport: str = "official"
    onebot_api_url: str = "http://127.0.0.1:3001"
    onebot_api_token: str = ""
    onebot_event_secret: str = ""
    qq_app_id: str = ""
    qq_secret: str = ""
    qq_bot_id: str = ""
    secure_cookie: bool = False
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")

    def __post_init__(self):
        if self.qq_transport not in {"official", "onebot"}:
            raise ValueError("QQ_TRANSPORT 必须是 official 或 onebot")
        for name, value in (
            ("OPENAI_BASE_URL", self.api_base_url),
            ("ONEBOT_API_URL", self.onebot_api_url),
        ):
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
            ):
                raise ValueError(f"{name} 必须是无内嵌凭证、查询参数和片段的 HTTP(S) 地址")

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            db_path=Path(os.getenv("DATABASE_PATH", str(ROOT / "data/bot.sqlite3"))),
            api_key=os.getenv("OPENAI_API_KEY", ""),
            api_base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
            qq_transport=os.getenv("QQ_TRANSPORT", "official"),
            onebot_api_url=os.getenv("ONEBOT_API_URL", "http://127.0.0.1:3001"),
            onebot_api_token=os.getenv("ONEBOT_API_TOKEN", ""),
            onebot_event_secret=os.getenv("ONEBOT_EVENT_SECRET", ""),
            qq_app_id=os.getenv("QQ_APP_ID", ""),
            qq_secret=os.getenv("QQ_APP_SECRET", ""),
            qq_bot_id=os.getenv("QQ_BOT_ID", ""),
            secure_cookie=os.getenv("SECURE_COOKIE", "false").lower() == "true",
            allowed_hosts=tuple(os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")),
        )


def create_app(config: Config | None = None, *, model=None, sender=None, start_worker=True):
    config = config or Config.from_env()
    store = Store(config.db_path)
    auth = Auth(store, config.secure_cookie)
    model = model if model is not None else OpenAIProvider(config.api_key, config.api_base_url)
    if sender is None:
        sender = (
            OneBotSender(config.onebot_api_url, config.onebot_api_token)
            if config.qq_transport == "onebot"
            else QQProvider(config.qq_app_id, config.qq_secret)
        )
    service = BotService(store, model, sender)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(service.worker()) if start_worker else None
        app.state.worker = task
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            for provider in (model, sender):
                if hasattr(provider, "close"):
                    await provider.close()

    app = FastAPI(
        title="群聊机器人管理后台",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.service = store, service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts))

    @app.middleware("http")
    async def request_boundary(request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.url.path not in {"/webhooks/qq", "/webhooks/onebot"}:
                origin = request.headers.get("origin")
                if origin and urlsplit(origin).netloc != request.headers.get("host"):
                    return JSONResponse({"detail": "不允许跨站修改"}, status_code=403)
                if request.headers.get("sec-fetch-site") == "cross-site":
                    return JSONResponse({"detail": "不允许跨站修改"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "需要 application/json"}, status_code=415)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return JSONResponse({"detail": "请求内容过大"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ProviderError)
    async def provider_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(ModelSelectionError)
    async def model_selection_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Do not echo submitted passwords or arbitrary request bodies in errors.
        issues = [
            ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
            for error in exc.errors()
        ]
        return JSONResponse({"detail": "；".join(issues)}, status_code=422)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "app/static/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        try:
            csrf = auth.session(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            csrf = None
        return {"initialized": auth.initialized(), "authenticated": csrf is not None, "csrf": csrf}

    @app.post("/api/auth/setup")
    def setup(payload: Credentials, request: Request, response: Response):
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "首次创建管理员需要从服务器本机访问；远程部署可使用 SSH 隧道")
        auth.setup(payload.password)
        return auth.login(payload.password, response)

    @app.post("/api/auth/login")
    def login(payload: Credentials, response: Response):
        return auth.login(payload.password, response)

    api = APIRouter(prefix="/api", dependencies=[Depends(auth.require)])

    @api.post("/auth/logout")
    def logout(request: Request, response: Response):
        auth.logout(request, response)
        return {"ok": True}

    @api.get("/status")
    async def status():
        task = getattr(app.state, "worker", None)
        with store.connect() as db:
            conversations = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            failed = db.execute(
                "SELECT COUNT(*) FROM runs WHERE status IN ('failed','delivery_failed')"
            ).fetchone()[0]
        onebot = config.qq_transport == "onebot"
        connection = {"online": None, "error": None}
        if onebot and isinstance(sender, OneBotSender):
            connection = await sender.connection_status()
        return {
            "openai_configured": bool(config.api_key),
            "model_endpoint": config.api_base_url,
            "qq_transport": config.qq_transport,
            "qq_configured": (
                bool(config.onebot_api_token and config.onebot_event_secret)
                if onebot
                else bool(config.qq_app_id and config.qq_secret)
            ),
            "qq_full_message_detection": onebot or bool(config.qq_bot_id),
            "onebot_online": connection["online"],
            "onebot_error": connection["error"],
            "worker_running": task is not None and not task.done(),
            "conversations": conversations,
            "messages": messages,
            "failed_runs": failed,
        }

    @api.get("/settings")
    def get_settings():
        return store.settings()

    @api.put("/settings")
    def put_settings(payload: Settings):
        return store.update_settings(payload)

    @api.get("/models/available")
    async def available_models():
        return await fetch_catalog(model)

    @api.post("/models/sync")
    async def sync_models():
        catalog = await fetch_catalog(model)
        return {"settings": store.sync_models(catalog["models"]), "warnings": catalog["warnings"]}

    @api.get("/conversations")
    def conversations(
        q: str = Query("", max_length=200),
        source: str = Query("", pattern="^(qq|console)?$"),
        offset: int = Query(0, ge=0),
        limit: int = Query(30, ge=1, le=100),
    ):
        return store.list_conversations(q, source, offset, limit)

    @api.post("/conversations", status_code=201)
    def new_conversation(payload: NewConversation):
        return store.create_conversation(payload.title)

    @api.get("/conversations/{conversation_id}")
    def conversation(
        conversation_id: str,
        before: int | None = Query(None, ge=1),
        limit: int = Query(50, ge=1, le=100),
    ):
        return store.detail(conversation_id, before, limit)

    @api.post("/conversations/{conversation_id}/messages", status_code=202)
    def message(conversation_id: str, payload: NewMessage):
        conversation = store.conversation(conversation_id)
        if conversation["source"] != "console":
            raise HTTPException(403, "QQ群会话仅供查看，请在测试会话中调试人格")
        try:
            return store.ingest(
                conversation_id=conversation_id,
                content=payload.content,
                sender="管理员",
                sender_id="admin",
                mentioned=True,
                model=payload.model,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/conversations/{conversation_id}/reset-context")
    def reset_context(conversation_id: str):
        store.reset_context(conversation_id)
        return {"ok": True}

    @api.get("/runs/{run_id}")
    def run(run_id: str):
        return store.run(run_id)

    app.include_router(api)

    @app.post("/webhooks/qq")
    async def qq_webhook(request: Request):
        if config.qq_transport != "official":
            raise HTTPException(503, "当前使用 OneBot 接入，官方 QQ 回调未启用")
        if not config.qq_app_id or not config.qq_secret:
            raise HTTPException(503, "QQ 凭证尚未配置")
        if request.headers.get("x-bot-appid") != config.qq_app_id:
            raise HTTPException(401, "QQ AppID 不匹配")
        body = await request.body()
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "无效 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "无效事件")
        key = signing_key(config.qq_secret)
        if payload.get("op") == 13:
            data = payload.get("d")
            if not isinstance(data, dict):
                raise HTTPException(400, "无效验证数据")
            token, timestamp = data.get("plain_token"), data.get("event_ts")
            if (
                not isinstance(token, str)
                or not isinstance(timestamp, str)
                or not re.fullmatch(r"[A-Za-z0-9_+/=-]{1,512}", token)
                or not re.fullmatch(r"[0-9]{1,20}", timestamp)
            ):
                raise HTTPException(400, "无效验证字段")
            # Challenges are opaque tokens, never JSON event bodies. Otherwise this
            # public endpoint could sign an attacker's forged event for later replay.
            return {"plain_token": token, "signature": key.sign((timestamp + token).encode()).hex()}
        try:
            signature = bytes.fromhex(request.headers.get("x-signature-ed25519", ""))
            timestamp = request.headers.get("x-signature-timestamp", "")
            if not timestamp:
                raise ValueError("missing timestamp")
            key.public_key().verify(signature, timestamp.encode() + body)
        except (InvalidSignature, ValueError) as exc:
            raise HTTPException(401, "QQ 回调签名无效") from exc
        if payload.get("t") not in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
            return {"op": 12}
        try:
            event = QQEvent.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(422, "QQ群消息字段不完整或格式无效") from exc
        message = event.d
        if message.author.bot:
            return {"op": 12}
        mentioned = event.t == "GROUP_AT_MESSAGE_CREATE"
        if event.t == "GROUP_MESSAGE_CREATE":
            if not config.qq_bot_id:
                raise HTTPException(503, "全量消息模式需要配置 QQ_BOT_ID，以准确识别 @机器人")
            mentioned = any(
                config.qq_bot_id in {user.id, user.member_openid} for user in message.mentions
            )
        conversation_id = store.qq_conversation(message.group_openid)
        content = message.content.strip() or "[此消息没有文本内容；当前版本仅处理文字]"
        store.ingest(
            conversation_id=conversation_id,
            content=content,
            sender=message.author.username or message.author.member_openid or message.author.id,
            sender_id=message.author.member_openid or message.author.id,
            mentioned=mentioned,
            external_id=message.id,
            timestamp=message.timestamp,
            mention_targets=[
                {"id": user.member_openid, "name": user.username or user.member_openid}
                for user in [message.author, *message.mentions]
                if user.member_openid
                and not user.bot
                and (not config.qq_bot_id or config.qq_bot_id not in {user.member_openid, user.id})
            ],
        )
        return {"op": 12}

    @app.post("/webhooks/onebot")
    async def onebot_webhook(request: Request):
        if config.qq_transport != "onebot" or not config.onebot_event_secret:
            raise HTTPException(503, "OneBot 上报尚未配置")
        body = await request.body()
        # NapCat reverse HTTP signs the exact body; it does not send a Bearer token.
        expected = (
            "sha1=" + hmac.new(config.onebot_event_secret.encode(), body, hashlib.sha1).hexdigest()
        )
        if not hmac.compare_digest(
            request.headers.get("x-signature", "").encode(), expected.encode()
        ):
            raise HTTPException(401, "OneBot 上报签名无效")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "无效 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "无效事件")
        if payload.get("post_type") != "message" or payload.get("message_type") != "group":
            return {"ok": True}
        try:
            event = GroupMessage.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(422, "OneBot 群消息字段不完整或格式无效") from exc
        if request.headers.get("x-self-id") != str(event.self_id):
            raise HTTPException(401, "OneBot 账号标识不匹配")
        if event.user_id == event.self_id:
            return {"ok": True}
        event.ingest(store)
        return {"ok": True}

    return app
