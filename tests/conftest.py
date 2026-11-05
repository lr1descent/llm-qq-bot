from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from app.main import Config, create_app
from app.providers import Generation
from app.schemas import Settings


@dataclass
class RecordingModel:
    calls: list = field(default_factory=list)
    error: Exception | None = None
    text: str = "来自测试替身的回答"

    async def generate(self, model, settings, context):
        self.calls.append((model, settings, context))
        if self.error:
            raise self.error
        return Generation(self.text, "test-response", 100, 20)

    async def models(self):
        return ["gpt-5.6-luna", "gpt-6.1-sol"]


@dataclass
class RecordingSender:
    calls: list = field(default_factory=list)
    error: Exception | None = None
    replies: list = field(default_factory=list)

    async def send(self, group, message_id, reply):
        self.replies.append(reply)
        self.calls.append((group, message_id, reply.text))
        if self.error:
            raise self.error


@pytest.fixture
def app_bundle(tmp_path):
    config = Config(
        db_path=tmp_path / "bot.sqlite3",
        qq_app_id="11111111",
        qq_secret="DG5g3B4j9X2KOErG",
        qq_bot_id="bot-open-id",
        allowed_hosts=("testserver", "localhost", "127.0.0.1"),
    )
    model, sender = RecordingModel(), RecordingSender()
    app = create_app(config, model=model, sender=sender, start_worker=False)
    settings = app.state.store.settings().model_dump()
    app.state.store.update_settings(
        Settings.model_validate(
            {
                **settings,
                "models": [
                    {"id": "gpt-5.6-luna", "enabled": True},
                    {"id": "gpt-6.1-sol", "enabled": True},
                ],
                "default_model": "gpt-5.6-luna",
            }
        )
    )
    return app, model, sender, config


@pytest.fixture
def client(app_bundle):
    app = app_bundle[0]
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client


@pytest.fixture
def admin(client):
    result = client.post("/api/auth/setup", json={"password": "test-password-12345"})
    assert result.status_code == 200
    client.headers["X-CSRF-Token"] = result.json()["csrf"]
    return client
