"""OneBot v11 event normalization and outbound HTTP API for NapCat."""

from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from app.mentions import Mention, Reply
from app.providers import ProviderError


class MessageSegment(BaseModel):
    type: str = Field(max_length=64)
    data: dict[str, Any]

    @model_validator(mode="after")
    def validate_content(self):
        if self.type == "text" and not isinstance(self.data.get("text"), str):
            raise ValueError("text 消息段缺少文字")
        if self.type == "at" and not isinstance(self.data.get("qq"), (str, int)):
            raise ValueError("at 消息段缺少 QQ 号")
        return self


class Sender(BaseModel):
    nickname: str = Field(default="", max_length=256)
    card: str = Field(default="", max_length=256)


class GroupMessage(BaseModel):
    post_type: Literal["message"]
    message_type: Literal["group"]
    time: int = Field(gt=0, strict=True)
    self_id: int = Field(gt=0, strict=True)
    user_id: int = Field(gt=0, strict=True)
    group_id: int = Field(gt=0, strict=True)
    message_id: int = Field(strict=True)
    message: list[MessageSegment] = Field(max_length=1000)
    sender: Sender

    @model_validator(mode="after")
    def validate_text_size_and_time(self):
        if len(self.content) > 12000:
            raise ValueError("群消息超过 12000 字符")
        try:
            datetime.fromtimestamp(self.time, UTC)
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("群消息时间戳无效") from exc
        return self

    @property
    def mentioned(self):
        return any(
            segment.type == "at" and str(segment.data["qq"]) == str(self.self_id)
            for segment in self.message
        )

    @property
    def content(self):
        parts = []
        for segment in self.message:
            if segment.type == "text":
                parts.append(segment.data["text"])
            elif segment.type == "at":
                if str(segment.data["qq"]) != str(self.self_id):
                    parts.append("@" + str(segment.data["qq"]))
            elif segment.type != "reply":
                parts.append("[非文本消息：" + segment.type + "]")
        return "".join(parts).strip() or "[此消息没有文本内容；当前版本仅处理文字]"

    def ingest(self, store):
        # Message IDs are scoped to an account; do not collide after a QQ account switch.
        conversation = store.qq_conversation(
            f"onebot:{self.self_id}:{self.group_id}", title=f"QQ群 · {self.group_id}"
        )
        return store.ingest(
            conversation_id=conversation,
            external_id=str(self.message_id),
            content=self.content,
            sender=self.sender.card or self.sender.nickname or str(self.user_id),
            sender_id=str(self.user_id),
            mentioned=self.mentioned,
            timestamp=datetime.fromtimestamp(self.time, UTC).isoformat(),
            mention_targets=[
                {"id": str(segment.data["qq"]), "name": str(segment.data["qq"])}
                for segment in self.message
                if segment.type == "at"
                and str(segment.data["qq"]).isascii()
                and str(segment.data["qq"]).isdigit()
                and str(segment.data["qq"]) != str(self.self_id)
            ],
        )


class OneBotSender:
    def __init__(self, base_url: str, token: str, *, http_client=None):
        self.token = token
        self.client = http_client or httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": "Bearer " + token}, timeout=20
        )

    async def call(self, action: str, payload: dict, *, timeout=20):
        if not self.token:
            raise ProviderError("尚未配置 ONEBOT_API_TOKEN")
        try:
            response = await self.client.post("/" + action, json=payload, timeout=timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"NapCat HTTP {exc.response.status_code}：请检查登录状态和 OneBot Token。"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError("NapCat 连接失败或超时；未自动重发，请检查登录和网络。") from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise ProviderError("NapCat 返回了无效 JSON") from exc
        if (
            not isinstance(result, dict)
            or result.get("status") != "ok"
            or result.get("retcode") != 0
        ):
            raise ProviderError("NapCat 拒绝请求，请查看 NapCat 日志；未自动重发。")
        if not isinstance(result.get("data"), dict):
            raise ProviderError("NapCat 未返回有效的结果数据")
        return result["data"]

    async def connection_status(self):
        try:
            data = await self.call("get_status", {}, timeout=3)
            return {"online": data.get("online") is True, "error": None}
        except ProviderError as exc:
            return {"online": False, "error": str(exc)}

    async def send(self, group, message_id, reply: Reply):
        parts = group.split(":")
        if len(parts) != 3 or parts[0] != "onebot" or not all(x.isdigit() for x in parts[1:]):
            raise ProviderError("这条消息属于其他 QQ 接入方式，未向 NapCat 发送。")
        account, group_id = parts[1:]
        login = await self.call("get_login_info", {})
        if str(login.get("user_id")) != account:
            raise ProviderError("NapCat 登录账号已变化；未使用新账号发送旧账号的回复。")
        data = await self.call(
            "send_group_msg",
            {
                "group_id": int(group_id),
                # Only resolved mentions become at segments; generated CQ strings stay text.
                "message": [
                    {"type": "reply", "data": {"id": message_id}},
                    *(
                        {"type": "at", "data": {"qq": part.id}}
                        if isinstance(part, Mention)
                        else {"type": "text", "data": {"text": part}}
                        for part in reply.parts
                    ),
                ],
            },
        )
        if data.get("message_id") in (None, ""):
            raise ProviderError("NapCat 未返回消息 ID，无法确认发送成功。")

    async def close(self):
        await self.client.aclose()
