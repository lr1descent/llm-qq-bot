import asyncio
import json

import httpx
import pytest

from app.mentions import MentionError, context_members, resolve_reply
from app.providers import QQProvider


@pytest.mark.parametrize(
    "text", ["[[at:unknown]]", "[[at:all]]", "[[at:123", "[[at:123]] [[at:unknown]]"]
)
def test_unknown_and_malformed_mentions_are_explicit_errors(text):
    with pytest.raises(MentionError):
        resolve_reply(text, {"123": "甲", "all": "所有人"})


def test_literal_nicknames_and_cq_strings_do_not_become_mentions():
    text = "@甲 [CQ:at,qq=all]"
    reply = resolve_reply(text, {"123": "甲"})
    assert reply.text == text
    assert not reply.mentions


@pytest.mark.parametrize(
    "example",
    ["`[[at:123]]`", "```text\n[[at:unknown]]\n```", "> [[at:123]]", "~~~\n[[at:all]]\n~~~"],
)
def test_quoted_mention_examples_remain_literal(example):
    reply = resolve_reply(example + "\n[[at:123]] 收到。", {"123": "甲"})
    assert len(reply.mentions) == 1
    assert reply.parts[0] == example
    assert reply.text.endswith("@甲 收到。")


def test_same_name_members_are_identified_by_id_and_later_numeric_ref_keeps_name():
    members = context_members(
        [
            {"mention_targets": [{"id": "123", "name": "小明"}, {"id": "456", "name": "小明"}]},
            {"mention_targets": [{"id": "123", "name": "123"}]},
        ]
    )
    reply = resolve_reply("[[at:456]]", members)
    assert reply.mentions[0].id == "456"
    assert members["123"] == "小明"


@pytest.mark.parametrize("text,expected_type", [("普通回答", 0), ("[[at:member-1]] 收到", 2)])
def test_official_qq_uses_markdown_only_for_mentions(text, expected_type):
    requests = []

    def handler(request):
        requests.append(request)
        data = (
            {"access_token": "test-token", "expires_in": 7200}
            if request.url.path.endswith("getAppAccessToken")
            else {"id": "sent-id"}
        )
        return httpx.Response(200, json=data)

    async def run():
        sender = QQProvider("test-app", "test-secret")
        await sender.client.aclose()
        sender.client = httpx.AsyncClient(
            base_url="https://api.bot.qq.com", transport=httpx.MockTransport(handler)
        )
        await sender.send("group-1", "msg-1", resolve_reply(text, {"member-1": "群友"}))
        await sender.close()

    asyncio.run(run())
    payload = json.loads(requests[-1].content)
    assert payload["msg_type"] == expected_type
    assert payload["msg_id"] == "msg-1"
    if expected_type == 2:
        assert payload["markdown"]["content"] == '<qqbot-at-user id="member-1" /> 收到'
        assert "content" not in payload
    else:
        assert payload["content"] == text
        assert "markdown" not in payload
