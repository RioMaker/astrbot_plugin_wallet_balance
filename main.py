"""Administrator-only wallet lookup; no LLM call is made."""

import asyncio
import time
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star

from .balance import (
    BalanceError,
    bounded_int,
    fetch_balance_snapshot,
    query_time,
    resolve_target,
)
from .consumption_reports import group_usage_line, summary_text
from .consumption_store import ConsumptionStore
from .pricing import normalize_usage, quote_usage
from .usage_tracking import ProviderUsageTracker

BEIJING = timezone(timedelta(hours=8))


def _field(obj, key, default=None):
    return (
        obj.get(key, default)
        if isinstance(obj, Mapping)
        else getattr(obj, key, default)
    )


def _today():
    return datetime.now(BEIJING).date().isoformat()


def _archive_path():
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    return (
        Path(get_astrbot_data_path())
        / "plugin_data"
        / "astrbot_plugin_wallet_balance"
        / "consumption.db"
    )


class WalletBalance(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._cache = {}
        self._lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self.store = None
        self.tracker = None
        self.observation_error = False

    async def initialize(self):
        try:
            self.store = await asyncio.to_thread(
                ConsumptionStore,
                _archive_path(),
                max_mib=bounded_int(self.config, "max_archive_mib", 64, 1, 1024),
            )
            self.tracker = ProviderUsageTracker(
                self.context, self._llm, self._tracking_error
            )
            if not self.tracker.install():
                self._tracking_error()
        except Exception:
            self._tracking_error()

    def _tracking_error(self, _message=None):
        # Upstream details can contain credentials. Keep diagnostics generic.
        self.observation_error = True
        logger.warning("[API 钱包余额] 群消耗统计接入或写入异常，记录可能不完整。")

    @staticmethod
    def _group_scope(event):
        group_getter = getattr(event, "get_group_id", None)
        group = str(group_getter() or "") if callable(group_getter) else ""
        if not group:
            return None
        platform = str(event.get_platform_id() or event.get_platform_name() or "")
        bot = str(event.get_self_id() or "")
        if not platform or not bot:
            raise ValueError("missing group scope")
        return platform, bot, group

    async def _llm(self, event, provider, response, failed, started_at):
        try:
            scope = self._group_scope(event)
            if not scope or self.store is None:
                return
            try:
                meta = provider.meta()
            except Exception:
                meta = None
            config = _field(provider, "provider_config", {}) or {}
            provider_id = str(
                _field(meta, "id") or _field(config, "id") or type(provider).__name__
            )
            raw_model = _field(_field(response, "raw_completion"), "model")
            model = str(
                raw_model
                or _field(response, "model")
                or _field(meta, "model")
                or _field(config, "model")
                or "未返回模型名"
            )
            usage = normalize_usage(response)
            quote = quote_usage(
                usage, model, _field(config, "api_base", ""), started_at
            )
            await asyncio.to_thread(
                self.store.record,
                platform=scope[0],
                bot=scope[1],
                group=scope[2],
                provider=provider_id,
                model=model,
                started_at=started_at,
                input_tokens=usage.input_tokens,
                cached_tokens=usage.cached_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cost_cny=quote.cost_cny,
                cost_usd=quote.cost_usd,
                tariff_id=quote.tariff_id,
                period=quote.period,
                reason=quote.reason,
                failed=failed or _field(response, "role") == "err",
            )
        except Exception:
            self._tracking_error()

    async def _statistics_state(self):
        tracker = self.tracker.status() if self.tracker else {}
        state = await asyncio.to_thread(self.store.status) if self.store else {}
        ready = bool(
            self.store and tracker.get("installed") and tracker.get("provider_classes")
        )
        incomplete = bool(
            self.observation_error
            or tracker.get("errors")
            or state.get("write_failures")
        )
        return ready, incomplete

    async def _group_today(self, event):
        """Read fresh usage independently of the cached account balance."""
        try:
            scope = self._group_scope(event)
            if not scope:
                return None
            ready, incomplete = await self._statistics_state()
            summary = (
                await asyncio.to_thread(
                    self.store.summary, scope[0], scope[1], group=scope[2], day=_today()
                )
                if self.store
                else None
            )
            return summary, ready, incomplete
        except Exception:
            self._tracking_error()
            return None, False, True

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
                    result = await fetch_balance_snapshot(target, timeout)
                    stamp = query_time()
                    if len(self._cache) >= 64:
                        self._cache.clear()
                    if ttl:
                        self._cache[target.cache_id] = (time.monotonic(), result, stamp)
            text = f"{target.label}\n{result.as_text()}\n查询时间：{stamp}"
            if hit:
                text += "（缓存）"
            text += "\n余额属于 API Key 对应账户；多 Key 时仅查询当前 Key，不累加。"
            group_data = await self._group_today(event)
            group_lines = {}
            if group_data is not None:
                summary, ready, incomplete = group_data
                group_lines = {
                    row.currency: group_usage_line(
                        summary, row.currency, ready=ready, incomplete=incomplete
                    )
                    for row in result.balances
                }
                text += "\n" + "\n".join(group_lines.values())
                text += "\n群费用按返回 Token 和官网时段价格估算；/群消耗汇总 可查存档及统计起点。"
            if self.config.get("output_mode", "image") == "image":
                try:
                    # Import lazily so an unavailable imaging dependency can still
                    # fall back to the validated balance text.
                    from .balance_renderer import render_balance_bubble

                    images = []
                    async with self._render_lock:
                        for row in result.balances:
                            png = await asyncio.to_thread(
                                render_balance_bubble,
                                currency=row.currency,
                                total=row.total,
                                is_available=result.is_available,
                                query_stamp=stamp,
                                cached=hit,
                                font_path=self.config.get("font_path") or None,
                                **(
                                    {"group_usage_text": group_lines[row.currency]}
                                    if group_lines
                                    else {}
                                ),
                            )
                            images.append(Image.fromBytes(png))
                    yield event.chain_result(images)
                    return
                except Exception:
                    logger.warning("[API 钱包余额] 气泡图片生成失败，已降级为文字。")
                    text = "气泡图片暂不可用，以下为文字余额：\n" + text
        except BalanceError as exc:
            text = str(exc)
        except Exception:
            # Exception details may contain request headers or credentials.
            logger.warning("[API 钱包余额] 查询异常，请核对插件配置及 AstrBot 兼容性。")
            text = "查询异常，请核对配置、提供商是否启用及 AstrBot 兼容性。"
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("群消耗汇总", alias={"群消耗"})
    async def group_consumption(
        self, event: AstrMessageEvent, period: str = "总览", page: int = 1
    ):
        """查询所有平台/机器人的群存档：总览、今日、累计或 YYYY-MM-DD，可附页码。"""
        event.stop_event()
        try:
            if page < 1:
                raise ValueError
            if period in ("总览", "累计"):
                day = None
            elif period == "今日":
                day = _today()
            else:
                day = date.fromisoformat(period).isoformat()
                if day != period:
                    raise ValueError
        except (ValueError, TypeError):
            yield event.plain_result(
                "用法：/群消耗汇总 [总览|今日|累计|YYYY-MM-DD] [页码]，例如 /群消耗汇总 今日 2"
            )
            return
        if self.store is None:
            yield event.plain_result(
                "群消耗存档未就绪，请检查插件加载日志；钱包余额查询仍可使用。"
            )
            return
        try:
            ready, incomplete = await self._statistics_state()
            summary = await asyncio.to_thread(
                self.store.summary, None, None, day=day, page=page
            )
            today = (
                await asyncio.to_thread(self.store.summary, None, None, day=_today())
                if period == "总览"
                else None
            )
            text = summary_text(
                summary,
                period_label=("累计" if day is None else day),
                today=today,
                ready=ready,
                incomplete=incomplete,
            )
        except Exception:
            self._tracking_error()
            text = "群消耗存档暂不可读取，请检查插件加载日志和数据目录。"
        yield event.plain_result(text)

    async def terminate(self):
        if self.tracker:
            self.tracker.uninstall()
        if self.store:
            await asyncio.to_thread(self.store.close)
            self.store = None
        self._cache.clear()
