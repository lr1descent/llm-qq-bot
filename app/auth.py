import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response

from app.store import Conflict, Store

COOKIE = "qq_admin"
SESSION_SECONDS = 12 * 60 * 60


def password_hash(password: str, salt: bytes | None = None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


class Auth:
    def __init__(self, store: Store, secure_cookie: bool):
        self.store = store
        self.secure_cookie = secure_cookie

    def initialized(self):
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM admin").fetchone() is not None

    def setup(self, password):
        hashed = password_hash(password)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM admin").fetchone():
                raise Conflict("管理员已创建，请登录")
            db.execute("INSERT INTO admin VALUES (1, ?)", (hashed,))

    def login(self, password, response: Response):
        with self.store.connect() as db:
            row = db.execute("SELECT password_hash FROM admin").fetchone()
        if row is None:
            raise HTTPException(401, "请先创建管理员")
        stored = row[0]
        candidate = password_hash(password, bytes.fromhex(stored.split(":")[0]))
        if not hmac.compare_digest(stored, candidate):
            raise HTTPException(401, "密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), csrf, time.time() + SESSION_SECONDS),
            )
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=self.secure_cookie,
            samesite="strict",
            max_age=SESSION_SECONDS,
        )
        return {"csrf": csrf}

    def session(self, request: Request):
        token = request.cookies.get(COOKIE, "")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT csrf FROM sessions WHERE token_hash=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        if row is None:
            raise HTTPException(401, "登录已过期，请重新登录")
        return row[0]

    def require(self, request: Request):
        csrf = self.session(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(csrf, supplied):
                raise HTTPException(403, "请求校验失败，请刷新页面")

    def logout(self, request: Request, response: Response):
        token = request.cookies.get(COOKIE, "")
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
        response.delete_cookie(COOKIE)
