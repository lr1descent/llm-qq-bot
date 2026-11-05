from unittest.mock import AsyncMock

from app.providers import ProviderError
from app.store import Store


def test_new_install_has_no_hardcoded_models(tmp_path):
    settings = Store(tmp_path / "new.sqlite3").settings()
    assert settings.models == []
    assert settings.default_model == ""


def test_sync_adds_and_removes_models_preserving_preferences(admin, app_bundle):
    settings = admin.get("/api/settings").json()
    settings["models"][1]["enabled"] = False
    admin.put("/api/settings", json=settings)
    app_bundle[1].models = AsyncMock(
        return_value=["new/model:latest", "gpt-6.1-sol", "gpt-5.6-luna", "new/model:latest", ""]
    )
    response = admin.post("/api/models/sync", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["warnings"] and "1" in data["warnings"][0]
    settings = data["settings"]
    assert settings["models"] == [
        {"id": "gpt-5.6-luna", "enabled": True},
        {"id": "gpt-6.1-sol", "enabled": False},
        {"id": "new/model:latest", "enabled": True},
    ]
    assert settings["default_model"] == "gpt-5.6-luna"
    assert (
        admin.post("/api/models/sync", json={}).json()["settings"]["revision"]
        == settings["revision"]
    )
    # The removed default is never silently replaced by the first returned model.
    app_bundle[1].models = AsyncMock(return_value=["new/model:latest"])
    settings = admin.post("/api/models/sync", json={}).json()["settings"]
    assert settings["models"] == [{"id": "new/model:latest", "enabled": True}]
    assert settings["default_model"] == ""
    conversation = admin.post("/api/conversations", json={"title": "no default"}).json()["id"]
    assert (
        admin.post(
            f"/api/conversations/{conversation}/messages", json={"content": "hi"}
        ).status_code
        == 422
    )
    settings["default_model"] = "new/model:latest"
    assert admin.put("/api/settings", json=settings).status_code == 200


def test_failure_never_erases_saved_config(admin, app_bundle):
    original = admin.get("/api/settings").json()
    for result in [ProviderError("upstream offline"), ["", " "]]:
        app_bundle[1].models = (
            AsyncMock(side_effect=result)
            if isinstance(result, Exception)
            else AsyncMock(return_value=result)
        )
        assert admin.post("/api/models/sync", json={}).status_code == 502
        assert admin.get("/api/settings").json() == original


def test_empty_catalog_is_valid_and_clears_default(admin, app_bundle):
    app_bundle[1].models = AsyncMock(return_value=[])
    result = admin.post("/api/models/sync", json={}).json()
    assert result["settings"]["models"] == []
    assert result["settings"]["default_model"] == ""
    from tests.test_qq import event_body, signed_post

    response = signed_post(admin, app_bundle[3], event_body())
    assert response.status_code == 422
    assert "默认模型" in response.json()["detail"]


def test_sync_conflicts_with_stale_edits_and_invalidates_queued_model(admin, app_bundle):
    stale = admin.get("/api/settings").json()
    conversation = admin.post("/api/conversations", json={"title": "queued"}).json()["id"]
    run = admin.post(f"/api/conversations/{conversation}/messages", json={"content": "hi"}).json()
    app_bundle[1].models = AsyncMock(return_value=["other"])
    assert admin.post("/api/models/sync", json={}).status_code == 200
    assert admin.put("/api/settings", json=stale).status_code == 409
    import asyncio

    asyncio.run(app_bundle[0].state.service.process_next())
    assert app_bundle[0].state.store.run(run["run_id"])["status"] == "failed"
    assert not app_bundle[1].calls


def test_sync_requires_authentication_and_csrf(client, app_bundle):
    assert client.post("/api/models/sync", json={}).status_code == 401
    client.post("/api/auth/setup", json={"password": "test-password-12345"})
    assert client.post("/api/models/sync", json={}).status_code == 403
