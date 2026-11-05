from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Credentials(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    password: str = Field(min_length=8, max_length=256)


class ModelRule(StrictModel):
    id: str = Field(pattern=r"^\S{1,256}$")
    enabled: bool


class Settings(StrictModel):
    revision: int = Field(ge=1)
    persona: str = Field(min_length=1, max_length=16000)
    models: list[ModelRule]
    default_model: str
    context_messages: int = Field(ge=1, le=100)
    max_output_tokens: int = Field(ge=256, le=16384)

    @model_validator(mode="after")
    def valid_routing(self):
        ids = [model.id for model in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("模型 ID 不能重复")
        allowed = {model.id for model in self.models if model.enabled}
        if self.default_model and self.default_model not in allowed:
            raise ValueError("默认模型必须在启用列表中")
        return self


class NewConversation(StrictModel):
    title: str = Field(min_length=1, max_length=120)


class NewMessage(StrictModel):
    content: str = Field(min_length=1, max_length=12000)
    model: str | None = Field(default=None, max_length=256)


class QQAuthor(BaseModel):
    id: str = ""
    member_openid: str = Field(default="", pattern=r"^(?:[A-Za-z0-9_-]{1,128})?$")
    username: str = ""
    bot: bool = False


class QQMessage(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    group_openid: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    content: str = Field(default="", max_length=12000)
    author: QQAuthor
    mentions: list[QQAuthor] = Field(default_factory=list, max_length=100)
    timestamp: str = Field(max_length=64)

    @field_validator("timestamp")
    @classmethod
    def valid_timestamp(cls, value):
        from datetime import datetime

        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("QQ timestamp 必须包含时区")
        return parsed.isoformat()


class QQEvent(BaseModel):
    op: Literal[0]
    t: str
    d: QQMessage
