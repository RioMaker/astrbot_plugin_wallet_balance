"""Administrator-only wallet lookup; no LLM call is made."""

import asyncio
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .balance import (
    BalanceError,
    bounded_int,
    fetch_balance,
    query_time,
    resolve_target,
)


class WalletBalance(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._cache = {}
        self._lock = asyncio.Lock()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("钱包余额", alias={"余额查询", "wallet"})
    async def wallet(self, event: AstrMessageEvent):
        """查询当前会话或指定服务商的钱包，仅限 AstrBot 管理员。"""
        event.stop_event()
        try:
            target = await resolve_target(
                self.context, self.config, event.unified_msg_origin
            )
            ttl = bounded_int(self.config, "cache_seconds", 30, 0, 300)
            timeout = bounded_int(self.config, "timeout_seconds", 10, 1, 60)
            async with self._lock:
                cached = self._cache.get(target.cache_id)
                hit = bool(ttl and cached and time.monotonic() - cached[0] < ttl)
                if hit:
                    _, result, stamp = cached
                else:
                    result = await fetch_balance(target, timeout)
                    stamp = query_time()
                    if len(self._cache) >= 64:
                        self._cache.clear()
                    if ttl:
                        self._cache[target.cache_id] = (time.monotonic(), result, stamp)
            text = f"{target.label}\n{result}\n查询时间：{stamp}"
            if hit:
                text += "（缓存）"
            text += "\n余额属于 API Key 对应账户；多 Key 时仅查询当前 Key，不累加。"
        except BalanceError as exc:
            text = str(exc)
        except Exception:
            # Exception details may contain request headers or credentials.
            logger.warning("[API 钱包余额] 查询异常，请核对插件配置及 AstrBot 兼容性。")
            text = "查询异常，请核对配置、提供商是否启用及 AstrBot 兼容性。"
        yield event.plain_result(text)

    async def terminate(self):
        self._cache.clear()
