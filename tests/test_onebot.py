import asyncio
import hashlib
import hmac
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.mentions import Reply, resolve_reply
from app.onebot import OneBotSender
from app.providers import ProviderError

EVENT_SECRET = "test-event-secret"


@pytest.fixture
def onebot_bundle(app_bundle):
    _, model, sender, config = app_bundle
    config = replace(
        config,
        qq_transport="onebot",
        onebot_api_token="test-api-token",
        onebot_event_secret=EVENT_SECRET,
    )
    app = create_app(config, model=model, sender=sender, start_worker=False)
    with TestClient(app) as client:
        yield client, app, model, sender


def event(*, message_id=101, group=200, account=900, user=100, segments=None):
    return {
        "post_type": "message",
        "message_type": "group",
        "time": 1793808000,
        "self_id": account,
        "group_id": group,
        "user_id": user,
        "message_id": message_id,
        "sender": {"nickname": "昵称", "card": "群名片"},
        "message": segments
        if segments is not None
        else [
            {"type": "at", "data": {"qq": str(account)}},
            {"type": "text", "data": {"text": " 你好 "}},
        ],
    }


def post(client, payload, *, signature=None, self_id=None):
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    signature = (
        signature or "sha1=" + hmac.new(EVENT_SECRET.encode(), body, hashlib.sha1).hexdigest()
    )
    return client.post(
        "/webhooks/onebot",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": signature,
            "X-Self-ID": str(self_id or payload.get("self_id", 900)),
        },
    )


def test_signed_events_queue_once_and_preserve_context(onebot_bundle):
    client, app, model, sender = onebot_bundle
    plain = event(message_id=100, segments=[{"type": "text", "data": {"text": "上下文"}}])
    assert post(client, plain).status_code == 200
    assert asyncio.run(app.state.service.process_next()) is False
    for _ in range(2):
        assert post(client, event()).status_code == 200
    assert asyncio.run(app.state.service.process_next()) is True
    assert asyncio.run(app.state.service.process_next()) is False
    assert [x["content"] for x in model.calls[0][2]] == ["上下文", "你好"]
    assert sender.calls == [("onebot:900:200", "101", "来自测试替身的回答")]


def test_hmac_and_account_header_reject_untrusted_events(onebot_bundle):
    client, app, _, _ = onebot_bundle
    assert post(client, event(), signature="sha1=bad").status_code == 401
    assert post(client, event(), self_id=901).status_code == 401
    assert client.post("/webhooks/onebot", json=event()).status_code == 401
    assert app.state.store.list_conversations("", "", 0, 10)["total"] == 0


def test_at_all_other_users_and_literal_cq_do_not_trigger(onebot_bundle):
    client, app, model, sender = onebot_bundle
    for i, segments in enumerate(
        [
            [{"type": "at", "data": {"qq": "all"}}],
            [{"type": "at", "data": {"qq": "901"}}],
            [{"type": "text", "data": {"text": "[CQ:at,qq=900]"}}],
        ]
    ):
        assert post(client, event(message_id=i, segments=segments)).status_code == 200
    assert asyncio.run(app.state.service.process_next()) is False
    assert not model.calls and not sender.calls


def test_self_private_and_meta_events_are_ignored(onebot_bundle):
    client, app, _, _ = onebot_bundle
    for payload in [
        event(user=900),
        {"post_type": "meta_event"},
        {"post_type": "message", "message_type": "private"},
        {"post_type": "message_sent", "message_type": "group"},
    ]:
        assert post(client, payload).status_code == 200
    assert app.state.store.list_conversations("", "", 0, 10)["total"] == 0


def test_group_and_account_isolation_even_when_message_ids_repeat(onebot_bundle):
    client, app, model, sender = onebot_bundle
    for group, account in [(200, 900), (201, 900), (200, 901)]:
        assert post(client, event(group=group, account=account)).status_code == 200
        assert asyncio.run(app.state.service.process_next()) is True
    assert len(model.calls) == 3
    assert all(len(call[2]) == 1 for call in model.calls)
    assert len({call[0] for call in sender.calls}) == 3


@pytest.mark.parametrize(
    "update",
    [
        {"message": "[CQ:at,qq=900]"},
        {"message": [{"type": "text", "data": {"text": 123}}]},
        {"message": [{"type": "at", "data": {}}]},
        {"message": [{"type": "text", "data": {"text": "x" * 12001}}]},
        {"time": 10**30},
        {"group_id": True},
    ],
)
def test_invalid_message_shapes_are_explicit(onebot_bundle, update):
    client, app, _, _ = onebot_bundle
    assert post(client, event() | update).status_code == 422
    assert app.state.store.list_conversations("", "", 0, 10)["total"] == 0


def test_official_callback_disabled_when_onebot_selected(onebot_bundle):
    assert onebot_bundle[0].post("/webhooks/qq", json={}).status_code == 503


def test_onebot_sender_uses_authenticated_structured_message():
    requests = []

    def handler(request):
        requests.append(request)
        data = {"user_id": 900} if request.url.path == "/get_login_info" else {"message_id": 555}
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    async def run():
        client = httpx.AsyncClient(
            base_url="http://napcat:3000",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer test-key"},
        )
        sender = OneBotSender("http://napcat:3000", "test-key", http_client=client)
        await sender.send("onebot:900:200", "-123", Reply(("[CQ:at,qq=all]",)))
        await sender.close()

    asyncio.run(run())
    assert requests[-1].headers["Authorization"] == "Bearer test-key"
    assert requests[-1].url.path == "/send_group_msg"
    assert json.loads(requests[-1].content) == {
        "group_id": 200,
        "message": [
            {"type": "reply", "data": {"id": "-123"}},
            {"type": "text", "data": {"text": "[CQ:at,qq=all]"}},
        ],
    }


def test_onebot_mentions_are_real_segments_and_preserve_surrounding_text():
    requests = []

    def handler(request):
        requests.append(request)
        data = {"user_id": 900} if request.url.path == "/get_login_info" else {"message_id": 556}
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    async def run():
        sender = OneBotSender(
            "http://napcat:3000",
            "test-key",
            http_client=httpx.AsyncClient(
                base_url="http://napcat:3000", transport=httpx.MockTransport(handler)
            ),
        )
        reply = resolve_reply(
            "请 [[at:100]] 和 [[at:101]] 看这里 [CQ:at,qq=all]", {"100": "甲", "101": "乙"}
        )
        await sender.send("onebot:900:200", "123", reply)
        await sender.close()

    asyncio.run(run())
    assert json.loads(requests[-1].content)["message"] == [
        {"type": "reply", "data": {"id": "123"}},
        {"type": "text", "data": {"text": "请 "}},
        {"type": "at", "data": {"qq": "100"}},
        {"type": "text", "data": {"text": " 和 "}},
        {"type": "at", "data": {"qq": "101"}},
        {"type": "text", "data": {"text": " 看这里 [CQ:at,qq=all]"}},
    ]


def test_reply_mentions_context_member_and_records_readable_history(onebot_bundle):
    client, app, model, sender = onebot_bundle
    assert (
        post(
            client,
            event(
                message_id=90, user=123, segments=[{"type": "text", "data": {"text": "我是小明"}}]
            ),
        ).status_code
        == 200
    )
    model.text = "[[at:123]] 收到。"
    assert post(client, event()).status_code == 200
    asyncio.run(app.state.service.process_next())
    assert sender.replies[0].mentions[0].id == "123"
    conversation = app.state.store.qq_conversation("onebot:900:200")
    detail = app.state.store.detail(conversation, None, 50)
    assert detail["messages"][-1]["content"] == "@群名片 收到。"
    assert detail["runs"][0]["status"] == "completed"


def test_reply_can_mention_user_named_in_real_at_segment(onebot_bundle):
    client, app, model, sender = onebot_bundle
    model.text = "[[at:333]] 你好。"
    assert (
        post(
            client,
            event(
                segments=[
                    {"type": "at", "data": {"qq": "900"}},
                    {"type": "at", "data": {"qq": "333"}},
                    {"type": "text", "data": {"text": "请和这个人打招呼"}},
                ]
            ),
        ).status_code
        == 200
    )
    asyncio.run(app.state.service.process_next())
    assert sender.replies[0].mentions[0].id == "333"


def test_member_from_another_group_cannot_be_mentioned(onebot_bundle):
    client, app, model, sender = onebot_bundle
    assert (
        post(
            client,
            event(group=201, user=444, segments=[{"type": "text", "data": {"text": "另一个群"}}]),
        ).status_code
        == 200
    )
    model.text = "[[at:444]] 你好。"
    assert post(client, event()).status_code == 200
    asyncio.run(app.state.service.process_next())
    assert not sender.calls
    conversation = app.state.store.qq_conversation("onebot:900:200")
    detail = app.state.store.detail(conversation, None, 50)
    assert detail["runs"][0]["status"] == "failed"
    assert "无法确认" in detail["runs"][0]["error"]


def test_cleared_context_does_not_reintroduce_old_member(onebot_bundle):
    client, app, model, sender = onebot_bundle
    assert (
        post(
            client,
            event(
                message_id=90, user=123, segments=[{"type": "text", "data": {"text": "旧上下文"}}]
            ),
        ).status_code
        == 200
    )
    conversation = app.state.store.qq_conversation("onebot:900:200")
    app.state.store.reset_context(conversation)
    model.text = "[[at:123]] 你好。"
    assert post(client, event()).status_code == 200
    asyncio.run(app.state.service.process_next())
    assert not sender.calls


@pytest.mark.parametrize("failure", ["account_changed", "http_error", "retcode", "missing_id"])
def test_sender_failures_never_report_delivery_or_retry(failure):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/get_login_info":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 901 if failure == "account_changed" else 900},
                },
            )
        if failure == "http_error":
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "status": "failed" if failure == "retcode" else "ok",
                "retcode": 1200 if failure == "retcode" else 0,
                "data": {},
            },
        )

    async def run():
        sender = OneBotSender(
            "http://napcat:3000",
            "test-key",
            http_client=httpx.AsyncClient(
                base_url="http://napcat:3000", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            with pytest.raises(ProviderError):
                await sender.send("onebot:900:200", "1", Reply(("hello",)))
        finally:
            await sender.close()

    asyncio.run(run())
    assert calls.count("/send_group_msg") == (0 if failure == "account_changed" else 1)
