import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.providers import ProviderError
from app.store import Store


def create_conversation(client, title="测试会话"):
    response = client.post("/api/conversations", json={"title": title})
    assert response.status_code == 201
    return response.json()["id"]


def send(client, conversation, content="你好", model=None):
    return client.post(
        f"/api/conversations/{conversation}/messages", json={"content": content, "model": model}
    )


def test_first_setup_auth_csrf_and_logout(client):
    assert client.get("/api/auth/status").json()["initialized"] is False
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/conversations").status_code == 401
    result = client.post("/api/auth/setup", json={"password": "test1234"})
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=strict" in result.headers["set-cookie"]
    assert client.post("/api/auth/setup", json={"password": "another-password"}).status_code == 409
    config = client.get("/api/settings").json()
    assert client.put("/api/settings", json=config).status_code == 403
    client.headers["X-CSRF-Token"] = result.json()["csrf"]
    assert client.post("/api/auth/logout", json={}).status_code == 200
    assert client.get("/api/settings").status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "test1234"}).status_code == 200


def test_remote_setup_and_cross_site_mutation_rejected(app_bundle):
    with TestClient(app_bundle[0], client=("203.0.113.5", 5000)) as remote:
        assert (
            remote.post("/api/auth/setup", json={"password": "test-password-12345"}).status_code
            == 403
        )
    with TestClient(app_bundle[0], client=("127.0.0.1", 5000)) as local:
        assert (
            local.post(
                "/api/auth/setup",
                headers={"Origin": "https://evil.example"},
                json={"password": "test-password-12345"},
            ).status_code
            == 403
        )
        assert (
            local.post(
                "/api/auth/setup", content="{}", headers={"Content-Type": "text/plain"}
            ).status_code
            == 415
        )
        assert local.get("/", headers={"host": "evil.example"}).status_code == 400


def test_validation_does_not_echo_password(client):
    secret = "secret!"
    response = client.post("/api/auth/setup", json={"password": secret})
    assert response.status_code == 422
    assert secret not in response.text


def test_settings_persistence_revision_and_model_invariants(admin, app_bundle):
    original = admin.get("/api/settings").json()
    updated = {**original, "persona": "你是一位冷静的学术伙伴。", "default_model": "gpt-6.1-sol"}
    result = admin.put("/api/settings", json=updated)
    assert result.status_code == 200
    assert result.json()["revision"] == original["revision"] + 1
    assert Store(app_bundle[3].db_path).settings().persona == updated["persona"]
    assert admin.put("/api/settings", json=original).status_code == 409
    current = result.json()
    assert (
        admin.put(
            "/api/settings", json={**current, "models": [{"id": "gpt-6.1-sol", "enabled": False}]}
        ).status_code
        == 422
    )
    assert (
        admin.put("/api/settings", json={**current, "models": current["models"] * 2}).status_code
        == 422
    )
    assert (
        admin.put("/api/settings", json={**current, "default_model": "not-allowed"}).status_code
        == 422
    )


def test_routing_cannot_be_bypassed_and_queue_rechecks_rules(admin, app_bundle):
    conversation = create_conversation(admin)
    assert send(admin, conversation, model="expensive-unlisted-model").status_code == 422
    store = app_bundle[0].state.store
    assert store.detail(conversation, None, 50)["messages"] == []
    result = send(admin, conversation, model="gpt-6.1-sol").json()
    settings = admin.get("/api/settings").json()
    settings["models"][1]["enabled"] = False
    assert admin.put("/api/settings", json=settings).status_code == 200
    asyncio.run(app_bundle[0].state.service.process_next())
    assert not app_bundle[1].calls
    assert store.run(result["run_id"])["status"] == "failed"
    assert "禁用" in store.run(result["run_id"])["error"]


def test_prompt_snapshot_history_and_context_isolation(admin, app_bundle):
    a, b = create_conversation(admin, "A群测试"), create_conversation(admin, "B群测试")
    run_a = send(admin, a, "A的秘密").json()["run_id"]
    send(admin, b, "B的提问")
    settings = admin.get("/api/settings").json()
    settings["persona"] = "新的人格"
    admin.put("/api/settings", json=settings)
    service = app_bundle[0].state.service
    asyncio.run(service.process_next())
    asyncio.run(service.process_next())
    first, second = app_bundle[1].calls
    assert first[1]["persona"] != "新的人格"
    assert [m["content"] for m in second[2]] == ["B的提问"]
    detail = admin.get(f"/api/conversations/{a}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    run = admin.get(f"/api/runs/{run_a}").json()
    assert run["input_tokens"] == 100
    assert run["response_id"] == "test-response"
    assert run["context"][0]["content"] == "A的秘密"
    assert run["settings"]["persona"] != "新的人格"
    send(admin, a, "第三个问题")
    asyncio.run(service.process_next())
    assert app_bundle[1].calls[-1][1]["persona"] == "新的人格"
    assert [m["role"] for m in app_bundle[1].calls[-1][2]] == ["user", "assistant", "user"]
    assert not app_bundle[2].calls


def test_queued_turn_does_not_see_future_messages(admin, app_bundle):
    conversation = create_conversation(admin)
    send(admin, conversation, "第一问")
    send(admin, conversation, "第二问")
    service = app_bundle[0].state.service
    asyncio.run(service.process_next())
    asyncio.run(service.process_next())
    assert [m["content"] for m in app_bundle[1].calls[0][2]] == ["第一问"]
    assert [m["content"] for m in app_bundle[1].calls[1][2]] == [
        "第一问",
        "来自测试替身的回答",
        "第二问",
    ]


def test_reset_context_keeps_history_and_excludes_old_messages(admin, app_bundle):
    conversation = create_conversation(admin)
    send(admin, conversation, "旧上下文")
    path = f"/api/conversations/{conversation}/reset-context"
    assert admin.post(path, json={}).status_code == 409
    asyncio.run(app_bundle[0].state.service.process_next())
    assert admin.post(path, json={}).status_code == 200
    send(admin, conversation, "新上下文")
    asyncio.run(app_bundle[0].state.service.process_next())
    assert [m["content"] for m in app_bundle[1].calls[-1][2]] == ["新上下文"]
    assert len(admin.get(f"/api/conversations/{conversation}").json()["messages"]) == 4


def test_search_pagination_and_full_message_content(admin, app_bundle):
    conversation = create_conversation(admin, "needle title")
    store = app_bundle[0].state.store
    for i in range(55):
        store.ingest(
            conversation_id=conversation,
            content=f"内容 {i} <script>alert(1)</script>",
            sender="用户",
            sender_id="user",
            mentioned=False,
        )
    listing = admin.get("/api/conversations", params={"q": "内容 54"}).json()
    assert listing["total"] == 1
    assert listing["items"][0]["message_count"] == 55
    assert admin.get("/api/conversations", params={"q": "%' OR 1=1 --"}).json()["total"] == 0
    page = admin.get(f"/api/conversations/{conversation}").json()
    assert page["has_more"] is True
    older = admin.get(
        f"/api/conversations/{conversation}", params={"before": page["messages"][0]["id"]}
    ).json()
    assert len(older["messages"]) == 5
    assert "<script>" in older["messages"][0]["content"]
    assert admin.get("/api/conversations/missing").status_code == 404


def test_provider_failure_is_recorded_without_fake_answer(admin, app_bundle):
    app_bundle[1].error = ProviderError("测试：账号额度不足")
    conversation = create_conversation(admin)
    run_id = send(admin, conversation).json()["run_id"]
    asyncio.run(app_bundle[0].state.service.process_next())
    run = admin.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "failed"
    assert "额度不足" in run["error"]
    assert len(admin.get(f"/api/conversations/{conversation}").json()["messages"]) == 1
    assert len(app_bundle[1].calls) == 1


def test_restart_keeps_admin_and_history_marks_interrupted_run(admin, app_bundle):
    conversation = create_conversation(admin)
    run_id = send(admin, conversation).json()["run_id"]
    store = app_bundle[0].state.store
    store.claim()
    restarted = create_app(
        app_bundle[3], model=app_bundle[1], sender=app_bundle[2], start_worker=False
    )
    restarted.state.store.recover()
    with TestClient(restarted) as client:
        result = client.post("/api/auth/login", json={"password": "test-password-12345"})
        assert result.status_code == 200
        assert client.get(f"/api/conversations/{conversation}").json()["messages"]
        run = client.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "failed"
        assert "未" in run["error"] or "没有" in run["error"]
