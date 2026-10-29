"""End-to-end local token-to-tariff-to-archive checks; no external API calls."""

import asyncio
import importlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import ModuleType
from types import SimpleNamespace as NS

import pytest
from test_balance import (
    PACKAGE,
    PAYLOAD,
    context,
    install_renderer,
    provider,
    snapshot_fetch,
    wallet_event,
)
from test_balance import (
    main as main,
)

TODAY = "2026-10-30"
PEAK = datetime(2026, 10, 30, 2, tzinfo=timezone.utc)  # 10:00 Beijing
OFF_PEAK = datetime(2026, 10, 30, 0, 59, 59, tzinfo=timezone.utc)


def group_event(platform="qq", bot="bot-a", group="group-a"):
    event = wallet_event()
    event.get_platform_id = lambda: platform
    event.get_platform_name = lambda: platform
    event.get_self_id = lambda: bot
    event.get_group_id = lambda: group
    return event


def priced_provider(base="https://api.deepseek.com/v1", model="deepseek-flash"):
    item = provider(base, key="synthetic-key-never-rendered")
    item.provider_config.update(id="provider-a", model=model)
    item.meta = lambda: NS(id="provider-a", model=model)
    return item


def actual_usage(scale=1, model="deepseek-flash", role="assistant"):
    # Actual structured Provider usage; no estimation from the synthetic message.
    return NS(
        usage=NS(input_other=999999, input_cached=0, output=999999),
        role=role,
        raw_completion=NS(
            model=model,
            usage=NS(
                prompt_tokens=100 * scale,
                prompt_cache_hit_tokens=20 * scale,
                prompt_cache_miss_tokens=80 * scale,
                completion_tokens=10 * scale,
                total_tokens=110 * scale,
                completion_tokens_details=NS(reasoning_tokens=7 * scale),
            ),
            choices=["synthetic-private-response-never-store"],
        ),
    )


class ReadyTracker:
    def __init__(self):
        self.installed = True

    def status(self):
        return {"installed": self.installed, "provider_classes": 1, "errors": []}

    def uninstall(self):
        self.installed = False


@pytest.fixture
def plugin(main, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_today", lambda: TODAY)
    result = main.WalletBalance(context(priced_provider()), {"output_mode": "text"})
    archive = main.ConsumptionStore(tmp_path / "consumption.db")
    result.store = archive
    result.tracker = ReadyTracker()
    yield result
    archive.close()


async def collect(iterator):
    return [item async for item in iterator]


def rows(store):
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute("SELECT * FROM buckets ORDER BY day, period")
        ]


def test_llm_records_reported_usage_and_request_start_official_tariff(plugin):
    event = group_event()
    p = priced_provider()

    async def run():
        await plugin._llm(event, p, actual_usage(), False, OFF_PEAK)
        await plugin._llm(event, p, actual_usage(), False, PEAK)

    asyncio.run(run())
    buckets = {row["period"]: row for row in rows(plugin.store)}
    assert set(buckets) == {"peak", "off_peak"}
    assert Decimal(buckets["off_peak"]["cost_cny"]) == Decimal("0.0001204")
    assert Decimal(buckets["peak"]["cost_cny"]) == Decimal("0.0002408")
    assert Decimal(buckets["peak"]["cost_usd"]) == Decimal("0.00003612")
    for row in buckets.values():
        assert (
            row["input_tokens"],
            row["cached_tokens"],
            row["output_tokens"],
            row["total_tokens"],
        ) == (100, 20, 10, 110)
        assert row["tariff_id"] == "deepseek-2026-10-29" and row["reason"] == ""
        assert row["provider"] == "provider-a" and row["model"] == "deepseek-flash"
        assert row["day"] == TODAY and row["unpriced_calls"] == 0
    database = plugin.store.path.read_bytes()
    assert b"synthetic-key-never-rendered" not in database
    assert b"synthetic-private-response-never-store" not in database
    assert not plugin.observation_error


def test_group_bot_platform_isolation_and_private_skip(plugin):
    identities = [
        ("qq", "bot-a", "group-a"),
        ("qq", "bot-a", "group-b"),
        ("qq", "bot-b", "group-a"),
        ("other", "bot-a", "group-a"),
    ]

    async def run():
        for scale, scope in enumerate(identities, start=1):
            await plugin._llm(
                group_event(*scope), priced_provider(), actual_usage(scale), False, PEAK
            )
        await plugin._llm(
            group_event(group=""), priced_provider(), actual_usage(100), False, PEAK
        )

    asyncio.run(run())
    for scale, (platform, bot, group) in enumerate(identities, start=1):
        summary = plugin.store.summary(platform, bot, group=group)
        assert summary["groups"] == 1
        assert summary["totals"]["total_tokens"] == 110 * scale
    total = plugin.store.summary(None, None)
    assert total["groups"] == 4 and total["totals"]["calls"] == 4
    assert total["totals"]["total_tokens"] == 1100


def test_unknown_usage_and_relay_keep_tokens_without_fabricating_prices(plugin):
    async def run():
        await plugin._llm(group_event(), priced_provider(), None, True, PEAK)
        await plugin._llm(
            group_event(),
            priced_provider("https://relay.example/v1"),
            actual_usage(),
            False,
            PEAK,
        )
        await plugin._llm(
            group_event(),
            priced_provider(model="unknown-model"),
            actual_usage(model="unknown-model"),
            False,
            PEAK,
        )

    asyncio.run(run())
    total = plugin.store.summary(None, None)["totals"]
    assert total["calls"] == 3 and total["failed_calls"] == 1
    assert total["missing_usage"] == 1 and total["unpriced_calls"] == 3
    assert total["total_tokens"] == 220
    assert total["cost_cny"] == total["cost_usd"] == Decimal(0)
    assert {row["reason"] for row in rows(plugin.store)} == {
        "用量不完整",
        "非官方接口",
        "未知模型",
    }
    text = asyncio.run(collect(plugin.group_consumption(group_event(), "累计")))[0]
    assert "220 Token" in text and "未估价" in text and "usage 缺失 1 次" in text
    assert "估算 ¥ 0.0000" not in text


def test_repeated_calls_on_different_days_remain_archived_after_reopen(main, plugin):
    async def run():
        for start in (PEAK - timedelta(days=1), PEAK, PEAK):
            await plugin._llm(
                group_event(), priced_provider(), actual_usage(), False, start
            )

    asyncio.run(run())
    path = plugin.store.path
    started_at = plugin.store.summary(None, None)["started_at"]
    plugin.store.close()
    reopened = main.ConsumptionStore(path)
    try:
        assert reopened.summary(None, None)["started_at"] == started_at
        assert reopened.summary(None, None, day="2026-10-29")["totals"]["calls"] == 1
        assert reopened.summary(None, None, day=TODAY)["totals"]["calls"] == 2
        assert reopened.summary(None, None)["totals"]["total_tokens"] == 330
        assert len(rows(reopened)) == 2
    finally:
        reopened.close()


def test_admin_overview_today_lifetime_dates_and_pagination(main, plugin):
    assert main.WalletBalance.group_consumption.permission == "admin"
    assert main.WalletBalance.wallet.permission == "admin"

    async def run():
        for index in range(12):
            await plugin._llm(
                group_event("qq", f"bot-{index % 2}", f"g-{index:02}"),
                priced_provider(),
                actual_usage(),
                False,
                PEAK,
            )
        await plugin._llm(
            group_event("other", "bot-z", "old-group"),
            priced_provider(),
            actual_usage(2),
            False,
            PEAK - timedelta(days=1),
        )
        viewer = group_event("unrelated", "unrelated", "")
        overview = (await collect(plugin.group_consumption(viewer)))[0]
        today = (await collect(plugin.group_consumption(viewer, "今日")))[0]
        cumulative = (await collect(plugin.group_consumption(viewer, "累计", 2)))[0]
        historical = (await collect(plugin.group_consumption(viewer, "2026-10-29")))[0]
        return overview, today, cumulative, historical

    overview, today, cumulative, historical = asyncio.run(run())
    assert "今日合计：12 群 · 1,320 Token" in overview
    assert "本期合计：13 群 · 1,540 Token" in overview
    assert "统计开始：" in overview and "北京时间" in overview
    assert "qq/bot-" in overview and "other/bot-z" in overview
    assert "期间：2026-10-30" in today and "本期合计：12 群 · 1,320 Token" in today
    assert "今日合计" not in today and "old-group" not in today
    assert "第 2/2 页" in cumulative and "本期合计：13 群 · 1,540 Token" in cumulative
    assert (
        "期间：2026-10-29" in historical and "本期合计：1 群 · 220 Token" in historical
    )
    assert "other/bot-z · 群 old-group" in historical


@pytest.mark.parametrize(
    "period,page",
    [
        ("today", 1),
        ("2026-02-30", 1),
        ("20261030", 1),
        ("2026-1-1", 1),
        ("今日", 0),
        ("今日", -1),
    ],
)
def test_admin_invalid_arguments_are_safe(plugin, period, page):
    event = group_event()
    result = asyncio.run(collect(plugin.group_consumption(event, period, page)))
    assert event.stopped and len(result) == 1
    assert result[0].startswith("用法：/群消耗汇总")


def test_balance_cache_does_not_cache_group_usage(main, plugin, monkeypatch):
    plugin.config["output_mode"] = "image"
    payload = {
        "is_available": True,
        "balance_infos": [
            PAYLOAD["balance_infos"][0],
            dict(PAYLOAD["balance_infos"][0], currency="USD"),
        ],
    }
    _, fetch_calls = snapshot_fetch(monkeypatch, main, payload)
    rendered = install_renderer(monkeypatch)
    monkeypatch.setattr(main, "query_time", lambda: "2026-10-30 10:00:00 北京时间")
    now = [100.0]
    monkeypatch.setattr(main, "time", NS(monotonic=lambda: now[0]))

    async def run():
        await plugin._llm(
            group_event(), priced_provider(), actual_usage(10000), False, PEAK
        )
        await plugin._llm(
            group_event(group="other-group"),
            priced_provider(),
            actual_usage(90000),
            False,
            PEAK,
        )
        assert len((await collect(plugin.wallet(group_event())))[0]) == 2
        await plugin._llm(
            group_event(), priced_provider(), actual_usage(10000), False, PEAK
        )
        now[0] = 110.0
        assert len((await collect(plugin.wallet(group_event())))[0]) == 2

    asyncio.run(run())
    assert len(fetch_calls) == 1
    assert [row["cached"] for row in rendered] == [False, False, True, True]
    assert len({row["query_stamp"] for row in rendered}) == 1
    assert [row["group_usage_text"] for row in rendered] == [
        "本群今日估算 ¥ 2.4080",
        "本群今日估算 0.3612 USD",
        "本群今日估算 ¥ 4.8160",
        "本群今日估算 0.7224 USD",
    ]


def test_private_balance_image_has_no_group_consumption(main, plugin, monkeypatch):
    plugin.config["output_mode"] = "image"
    snapshot_fetch(monkeypatch, main)
    rendered = install_renderer(monkeypatch)
    asyncio.run(collect(plugin.wallet(group_event(group=""))))
    assert "group_usage_text" not in rendered[0]


@pytest.mark.parametrize("failure", ["closed_archive", "tracker_status", "write"])
def test_statistics_failures_leave_wallet_balance_available(
    main, plugin, monkeypatch, failure
):
    snapshot_fetch(monkeypatch, main)
    warnings = []
    monkeypatch.setattr(main, "logger", NS(warning=warnings.append))

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-secret-not-for-log")

    if failure == "closed_archive":
        plugin.store.close()
    elif failure == "tracker_status":
        monkeypatch.setattr(plugin.tracker, "status", fail)
    else:
        monkeypatch.setattr(plugin.store, "record", fail)
        asyncio.run(
            plugin._llm(group_event(), priced_provider(), actual_usage(), False, PEAK)
        )
    result = asyncio.run(collect(plugin.wallet(group_event())))[0]
    assert "CNY 总余额：110.00" in result and "群消耗" in result
    assert "记录不完整" in result
    assert "synthetic-secret-not-for-log" not in repr(warnings + [result])
    assert plugin.observation_error


def test_unready_archive_and_admin_error_keep_balance_function(main, monkeypatch):
    snapshot_fetch(monkeypatch, main)
    item = main.WalletBalance(context(priced_provider()), {"output_mode": "text"})

    async def run():
        wallet = (await collect(item.wallet(group_event())))[0]
        admin = (await collect(item.group_consumption(group_event())))[0]
        return wallet, admin

    wallet, admin = asyncio.run(run())
    assert "110.00" in wallet and "统计未就绪" in wallet
    assert "存档未就绪" in admin


def test_initialize_pipeline_observation_and_terminate_preserve_archive(
    main, tmp_path, monkeypatch
):
    path = tmp_path / "actual-archive.db"
    monkeypatch.setattr(main, "_archive_path", lambda: path)
    monkeypatch.setattr(main, "_today", lambda: TODAY)

    class Provider:
        provider_config = {
            "api_base": "https://api.deepseek.com",
            "model": "deepseek-flash",
        }

        def meta(self):
            return NS(id="actual-provider", model="deepseek-flash")

        async def text_chat(self):
            return actual_usage()

        async def text_chat_stream(self):
            yield NS(
                is_chunk=True, usage=NS(input_other=900, input_cached=0, output=100)
            )
            final = actual_usage()
            del final.raw_completion.model
            final.model = "deepseek-v4-pro"
            final.is_chunk = False
            yield final

    class Pipeline:
        async def execute(self, event):
            return await event.action()

    original_execute, original_chat = Pipeline.execute, Provider.text_chat
    scheduler_module = ModuleType("astrbot.core.pipeline.scheduler")
    scheduler_module.PipelineScheduler = Pipeline
    monkeypatch.setitem(sys.modules, scheduler_module.__name__, scheduler_module)
    tracker_module = importlib.import_module(f"{PACKAGE}.usage_tracking")
    monkeypatch.setattr(tracker_module, "datetime", NS(now=lambda tz: PEAK))
    p = Provider()
    item_context = NS(get_all_providers=lambda: [p])
    item = main.WalletBalance(item_context, {})

    async def run():
        await item.initialize()
        assert item.tracker.status()["installed"] and not item.observation_error
        assert path.is_file()
        event = group_event()

        async def action():
            result = await p.text_chat()
            parts = [part async for part in p.text_chat_stream()]
            assert result.raw_completion.model == "deepseek-flash" and len(parts) == 2
            return "original-result"

        event.action = action
        assert await Pipeline().execute(event) == "original-result"
        summary = item.store.summary(None, None)
        assert summary["totals"]["total_tokens"] == 220
        # The stream's response.model must override the provider's flash default.
        assert summary["totals"]["cost_cny"] == Decimal("0.0012368")
        assert {row["model"] for row in rows(item.store)} == {
            "deepseek-flash",
            "deepseek-v4-pro",
        }
        stamp = summary["started_at"]
        await item.terminate()
        assert item.store is None and not item._cache
        assert not item.tracker.status()["installed"]
        assert (
            Pipeline.execute is original_execute and Provider.text_chat is original_chat
        )
        return stamp

    stamp = asyncio.run(run())
    reopened = main.ConsumptionStore(path)
    try:
        assert reopened.summary(None, None)["started_at"] == stamp
        assert reopened.summary(None, None)["totals"]["total_tokens"] == 220
    finally:
        reopened.close()


def test_initialize_storage_failure_does_not_disable_wallet(main, monkeypatch):
    warnings = []
    monkeypatch.setattr(main, "logger", NS(warning=warnings.append))
    monkeypatch.setattr(main, "_archive_path", lambda: "unused")

    def fail(*args, **kwargs):
        raise OSError("synthetic-secret-storage-path")

    monkeypatch.setattr(main, "ConsumptionStore", fail)
    snapshot_fetch(monkeypatch, main)
    item = main.WalletBalance(context(priced_provider()), {"output_mode": "text"})

    async def run():
        await item.initialize()
        assert item.store is None and item.observation_error
        result = (await collect(item.wallet(group_event())))[0]
        await item.terminate()
        return result

    result = asyncio.run(run())
    assert "110.00" in result
    assert "synthetic-secret-storage-path" not in repr(warnings + [result])
