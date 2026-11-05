import asyncio
import logging
import time
from dataclasses import replace

from app.mentions import MentionError, context_members, resolve_reply
from app.providers import GroupSender, LanguageModel, ProviderError
from app.store import Store

logger = logging.getLogger(__name__)
CONTEXT_CHAR_BUDGET = 64000


class BotService:
    def __init__(self, store: Store, model: LanguageModel, sender: GroupSender):
        self.store, self.model, self.sender = store, model, sender

    async def process_next(self):
        run = self.store.claim()
        if run is None:
            return False
        delivery = False
        try:
            # A queued request cannot bypass a model disabled after it was submitted.
            allowed = {rule.id for rule in self.store.settings().models if rule.enabled}
            if run["model"] not in allowed:
                raise ProviderError("该模型已被管理员禁用，请选择允许的模型重新提问。")
            context, size = [], 0
            for message in reversed(self.store.context(run)):
                if size + len(message["content"]) > CONTEXT_CHAR_BUDGET:
                    break
                context.append(message)
                size += len(message["content"])
            context.reverse()
            if not context:
                raise ProviderError("没有可用的会话上下文")
            self.store.save_context(run["id"], context)
            started = time.monotonic()
            result = await self.model.generate(run["model"], run["settings"], context)
            conversation = self.store.conversation(run["conversation_id"])
            reply = (
                resolve_reply(result.text, context_members(context))
                if conversation["source"] == "qq"
                else None
            )
            self.store.save_reply(
                run,
                replace(result, text=reply.text) if reply else result,
                round((time.monotonic() - started) * 1000),
                mentions=reply.mentions if reply else (),
            )
            if conversation["source"] == "qq":
                delivery = True
                trigger = next(item for item in context if item["id"] == run["trigger_id"])
                await self.sender.send(conversation["external_id"], trigger["external_id"], reply)
            self.store.finish(run["id"], "completed", delivery="sent" if delivery else "local")
        except (ProviderError, MentionError) as exc:
            self.store.finish(
                run["id"],
                "delivery_failed" if delivery else "failed",
                str(exc),
                delivery="failed" if delivery else None,
            )
            logger.warning("Run %s: %s", run["id"], exc)
        except asyncio.CancelledError:
            self.store.finish(run["id"], "failed", "服务停止，处理已中断；未自动重试。")
            raise
        except Exception:
            # The worker boundary records unexpected failures and retains a traceback.
            self.store.finish(
                run["id"],
                "delivery_failed" if delivery else "failed",
                "内部处理错误，请查看服务器日志",
                delivery="failed" if delivery else None,
            )
            logger.exception("Unexpected failure in run %s", run["id"])
        return True

    async def worker(self):
        self.store.recover()
        while True:
            if not await self.process_next():
                await asyncio.sleep(0.4)
