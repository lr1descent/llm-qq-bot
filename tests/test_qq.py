import asyncio
import json

from app.providers import ProviderError, signing_key
from app.store import now


def event_body(
    event_type="GROUP_AT_MESSAGE_CREATE", message_id="message-1", group="group-1", mentions=None
):
    return {
        "op": 0,
        "t": event_type,
        "d": {
            "id": message_id,
            "group_openid": group,
            "author": {"id": "member-1", "member_openid": "member-1", "username": "群友"},
            "content": "群里的消息",
            "timestamp": now(),
            "mentions": mentions or [],
        },
    }


def signed_post(client, config, payload):
    body = json.dumps(payload).encode()
    timestamp = "1793808000"
    signature = signing_key(config.qq_secret).sign(timestamp.encode() + body).hex()
    return client.post(
        "/webhooks/qq",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Bot-Appid": config.qq_app_id,
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": signature,
        },
    )


def test_callback_validation_matches_official_test_vector(client, app_bundle):
    response = client.post(
        "/webhooks/qq",
        json={"op": 13, "d": {"plain_token": "Arq0D5A61EgUu4OxUvOp", "event_ts": "1725442341"}},
        headers={"X-Bot-Appid": "11111111"},
    )
    assert response.status_code == 200
    assert (
        response.json()["signature"]
        == "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706"
    )


def test_invalid_signature_and_app_id_rejected(client, app_bundle):
    assert client.post("/webhooks/qq", json=event_body()).status_code == 401
    assert (
        client.post(
            "/webhooks/qq", json=event_body(), headers={"X-Bot-Appid": "11111111"}
        ).status_code
        == 401
    )
    assert not app_bundle[0].state.store.list_conversations("", "", 0, 20)["items"]


def test_challenge_cannot_be_used_to_sign_forged_event(client):
    response = client.post(
        "/webhooks/qq",
        json={"op": 13, "d": {"plain_token": json.dumps(event_body()), "event_ts": "1725442341"}},
        headers={"X-Bot-Appid": "11111111"},
    )
    assert response.status_code == 400
    response = client.post(
        "/webhooks/qq",
        json={"op": 13, "d": {"plain_token": "token", "event_ts": '{"op":0}'}},
        headers={"X-Bot-Appid": "11111111"},
    )
    assert response.status_code == 400


def test_mention_dedup_and_delivery(client, app_bundle):
    app, model, sender, config = app_bundle
    for _ in range(2):
        assert signed_post(client, config, event_body()).status_code == 200
    assert asyncio.run(app.state.service.process_next()) is True
    assert asyncio.run(app.state.service.process_next()) is False
    assert len(model.calls) == len(sender.calls) == 1
    assert sender.calls[0][:2] == ("group-1", "message-1")
    conversation = app.state.store.list_conversations("", "qq", 0, 20)["items"][0]
    assert (
        app.state.store.detail(conversation["id"], None, 50)["messages"][-1]["delivery"] == "sent"
    )


def test_official_author_and_mentioned_members_are_available(client, app_bundle):
    app, model, sender, config = app_bundle
    model.text = "[[at:member-2]] 收到。"
    payload = event_body(mentions=[{"member_openid": "member-2", "username": "小明"}])
    assert signed_post(client, config, payload).status_code == 200
    asyncio.run(app.state.service.process_next())
    assert sender.replies[0].mentions[0].id == "member-2"
    assert {m["id"] for m in model.calls[0][2][0]["mention_targets"]} == {"member-1", "member-2"}


def test_full_messages_are_context_only_until_bot_mentioned(client, app_bundle):
    app, model, sender, config = app_bundle
    signed_post(
        client,
        config,
        event_body(
            "GROUP_MESSAGE_CREATE", "plain", mentions=[{"id": "somebody-else", "bot": True}]
        ),
    )
    assert asyncio.run(app.state.service.process_next()) is False
    signed_post(
        client,
        config,
        event_body("GROUP_MESSAGE_CREATE", "at-bot", mentions=[{"id": "bot-open-id", "bot": True}]),
    )
    asyncio.run(app.state.service.process_next())
    assert len(model.calls) == 1
    assert len(model.calls[0][2]) == 2
    assert len(sender.calls) == 1


def test_qq_group_isolation_and_no_console_send(admin, app_bundle):
    app, model, _, config = app_bundle
    signed_post(admin, config, event_body(group="a"))
    signed_post(admin, config, event_body(group="b"))
    asyncio.run(app.state.service.process_next())
    asyncio.run(app.state.service.process_next())
    assert all(len(call[2]) == 1 for call in model.calls)
    conversation = app.state.store.list_conversations("", "qq", 0, 20)["items"][0]
    assert (
        admin.post(
            f"/api/conversations/{conversation['id']}/messages", json={"content": "不能发到群"}
        ).status_code
        == 403
    )


def test_delivery_failure_excludes_unsent_reply_from_context(client, app_bundle):
    app, model, sender, config = app_bundle
    sender.error = ProviderError("QQ 发送失败")
    signed_post(client, config, event_body())
    asyncio.run(app.state.service.process_next())
    conversation = app.state.store.list_conversations("", "qq", 0, 20)["items"][0]
    detail = app.state.store.detail(conversation["id"], None, 50)
    assert detail["runs"][0]["status"] == "delivery_failed"
    signed_post(client, config, event_body(message_id="next"))
    sender.error = None
    asyncio.run(app.state.service.process_next())
    assert all(message["role"] == "user" for message in model.calls[-1][2])
