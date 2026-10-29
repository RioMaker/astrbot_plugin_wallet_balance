"""Offline tests of daily archives, historical costs, and transactional limits."""

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from consumption_store import (  # noqa: E402
    ConsumptionStore,
    StorageCapacityError,
    StorageError,
)

BEFORE_MIDNIGHT = datetime(2026, 10, 29, 15, 59, 59, tzinfo=timezone.utc)
AFTER_MIDNIGHT = BEFORE_MIDNIGHT + timedelta(seconds=1)


@pytest.fixture
def store(tmp_path):
    result = ConsumptionStore(tmp_path / "consumption.sqlite3")
    yield result
    result.close()


def record(store, **overrides):
    values = dict(
        platform="qq",
        bot="bot-a",
        group="group-a",
        provider="provider-a",
        model="deepseek-chat",
        started_at=BEFORE_MIDNIGHT,
        input_tokens=100,
        cached_tokens=20,
        output_tokens=30,
        total_tokens=130,
        cost_cny=Decimal("0.00012345"),
        cost_usd=Decimal("0.00002345"),
        tariff_id="tariff-1",
        period="peak",
    )
    values.update(overrides)
    store.record(**values)


def rows(store):
    with closing(sqlite3.connect(store.path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM buckets ORDER BY day")]


def test_empty_archive_has_durable_start_and_zero_totals(store):
    result = store.summary("qq", "bot-a")
    assert result["started_at"] == store.status()["started_at"]
    assert datetime.fromisoformat(result["started_at"]).utcoffset() is not None
    assert result["entries"] == [] and result["groups"] == 0
    assert result["day"] is None and result["page"] == result["pages"] == 1
    assert result["totals"]["calls"] == 0
    assert result["totals"]["cost_cny"] == Decimal(0)
    assert isinstance(result["totals"]["cost_usd"], Decimal)
    assert rows(store) == []


def test_days_remain_archived_and_beijing_boundary_uses_request_start(store):
    record(store, started_at=AFTER_MIDNIGHT, total_tokens=5)
    record(store, started_at=BEFORE_MIDNIGHT, total_tokens=7)
    record(store, started_at=BEFORE_MIDNIGHT, total_tokens=11)
    assert [row["day"] for row in rows(store)] == ["2026-10-29", "2026-10-30"]
    old = store.summary("qq", "bot-a", day="2026-10-29")
    new = store.summary("qq", "bot-a", day="2026-10-30")
    total = store.summary("qq", "bot-a")
    assert (old["totals"]["calls"], old["totals"]["total_tokens"]) == (2, 18)
    assert (new["totals"]["calls"], new["totals"]["total_tokens"]) == (1, 5)
    assert (total["totals"]["calls"], total["totals"]["total_tokens"]) == (3, 23)
    assert store.summary("qq", "bot-a", day="2026-10-28")["totals"]["calls"] == 0


def test_groups_platforms_and_bots_are_isolated(store):
    record(store, total_tokens=1)
    record(store, group="group-b", total_tokens=2)
    record(store, platform="other", total_tokens=4)
    record(store, bot="other", total_tokens=8)
    summary = store.summary("qq", "bot-a")
    assert summary["groups"] == 2 and summary["totals"]["total_tokens"] == 3
    assert store.summary("qq", "bot-a", group="group-a")["totals"]["total_tokens"] == 1
    assert store.summary("qq", "bot-a", group="group-b")["totals"]["total_tokens"] == 2
    assert store.summary("other", "bot-a")["totals"]["total_tokens"] == 4
    assert store.summary("qq", "other")["totals"]["total_tokens"] == 8


def test_all_scope_keeps_same_group_id_separate_across_platform_and_bot(store):
    record(store, total_tokens=1)
    record(store, platform="other", total_tokens=2)
    record(store, bot="other", total_tokens=4)
    result = store.summary(None, None)
    assert result["platform"] is None and result["bot"] is None
    assert result["groups"] == 3 and result["totals"]["total_tokens"] == 7
    assert {
        (entry["platform"], entry["bot"], entry["group"], entry["total_tokens"])
        for entry in result["entries"]
    } == {
        ("qq", "bot-a", "group-a", 1),
        ("other", "bot-a", "group-a", 2),
        ("qq", "other", "group-a", 4),
    }
    assert store.summary("qq", None)["groups"] == 2
    assert store.summary(None, "bot-a")["totals"]["total_tokens"] == 3
    assert store.summary(None, None, group="group-a")["groups"] == 3
    assert store.summary("qq", "bot-a")["entries"][0]["total_tokens"] == 1


def test_tariff_period_model_provider_and_reason_preserve_separate_buckets(store):
    changes = [
        {},
        {"tariff_id": "tariff-2"},
        {"period": "valley"},
        {"provider": "provider-b"},
        {"model": "deepseek-reasoner"},
        {"reason": "cache usage absent"},
    ]
    for index, fields in enumerate(changes, start=1):
        record(store, cost_cny=Decimal(index), cost_usd=Decimal(index) / 10, **fields)
    before = rows(store)
    record(store, tariff_id="tariff-3", cost_cny=Decimal("99"))
    after = rows(store)
    assert len(before) == 6 and len(after) == 7
    assert all(row in after for row in before)
    totals = store.summary("qq", "bot-a")["totals"]
    assert totals["calls"] == 7 and totals["cost_cny"] == Decimal("120")


def test_unknown_usage_and_unpriced_calls_are_explicit_and_independent(store):
    record(
        store,
        input_tokens=None,
        cached_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_cny=None,
        cost_usd=None,
        failed=True,
    )
    record(
        store,
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_cny=Decimal(0),
        cost_usd=Decimal(0),
    )
    record(store, cost_cny=Decimal("0.1"), cost_usd=None)
    totals = store.summary("qq", "bot-a")["totals"]
    assert totals["calls"] == 3 and totals["failed_calls"] == 1
    assert totals["missing_usage"] == 1 and totals["unpriced_calls"] == 2
    assert totals["total_tokens"] == 130 and totals["cached_tokens"] == 20
    assert totals["cost_cny"] == Decimal("0.1") and totals["cost_usd"] == Decimal(0)


def test_decimal_accumulation_ignores_ambient_rounding_and_never_uses_real(store):
    cost = Decimal("12345678901234567890.123456789012345678901234567890")
    tiny = Decimal("0.000000000000000000000000000001")
    with localcontext() as context:
        context.prec = 6
        record(store, cost_cny=cost, cost_usd=tiny)
        record(store, cost_cny=tiny, cost_usd=tiny)
        result = store.summary("qq", "bot-a")["totals"]
    assert result["cost_cny"] == Decimal(
        "12345678901234567890.123456789012345678901234567891"
    )
    assert result["cost_usd"] == Decimal("0.000000000000000000000000000002")
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute(
            "SELECT typeof(cost_cny),typeof(cost_usd),typeof(total_tokens) FROM buckets"
        ).fetchone() == ("text", "text", "integer")


def test_sorting_uses_exact_decimal_and_pagination_keeps_full_totals(store):
    for group, cost in (("z", "9.99"), ("b", "10.0"), ("a", "10"), ("tiny", "0.01")):
        record(store, group=group, cost_cny=Decimal(cost))
    first = store.summary("qq", "bot-a", page_size=2)
    last = store.summary("qq", "bot-a", page=999, page_size=2)
    assert [item["group"] for item in first["entries"]] == ["a", "b"]
    assert [item["group"] for item in last["entries"]] == ["z", "tiny"]
    assert first["groups"] == 4 and last["page"] == last["pages"] == 2
    assert first["totals"] == last["totals"] and first["totals"]["cost_cny"] == Decimal(
        "30"
    )


def test_restart_preserves_original_start_historical_amounts_and_failure_status(store):
    record(store)
    before = store.summary("qq", "bot-a")
    started = store.status()["started_at"]
    store.close()
    second = ConsumptionStore(store.path)
    try:
        assert second.summary("qq", "bot-a") == before
        assert second.status()["started_at"] == started
    finally:
        second.close()


def test_threads_and_connections_do_not_lose_decimal_or_token_updates(store):
    second = ConsumptionStore(store.path)
    try:

        def write(index):
            record(store if index % 2 else second, cost_cny=Decimal("0.00000001"))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(120)))
        totals = store.summary("qq", "bot-a")["totals"]
        assert totals["calls"] == 120 and totals["total_tokens"] == 15600
        assert totals["cost_cny"] == Decimal("0.00000120")
        assert len(rows(store)) == 1
    finally:
        second.close()


def test_integer_overflow_rolls_back_money_and_every_counter(store):
    record(store, total_tokens=(1 << 63) - 1)
    before = store.summary("qq", "bot-a")
    with pytest.raises(StorageError, match="integer range"):
        record(store, total_tokens=1)
    assert store.summary("qq", "bot-a") == before
    assert store.status()["write_failures"] == 1
    with closing(sqlite3.connect(store.path)) as conn:
        assert (
            conn.execute("SELECT typeof(total_tokens) FROM buckets").fetchone()[0]
            == "integer"
        )
    store.close()
    second = ConsumptionStore(store.path)
    try:
        assert second.status()["write_failures"] == 1
        assert "integer range" in second.status()["last_error"]
    finally:
        second.close()


def test_read_totals_can_exceed_int64_without_sqlite_promotion(store):
    record(store, total_tokens=(1 << 63) - 1)
    record(store, group="group-b", total_tokens=(1 << 63) - 1)
    assert store.summary("qq", "bot-a")["totals"]["total_tokens"] == 2 * ((1 << 63) - 1)


def test_long_metadata_hashes_stay_bounded_and_distinct(store):
    for suffix in ("a", "b"):
        record(
            store,
            provider="p" * 1000 + suffix,
            model="m" * 1000 + suffix,
            tariff_id="t" * 1000 + suffix,
            period="v" * 1000 + suffix,
            reason="r" * 1000 + suffix,
        )
    saved = rows(store)
    assert len(saved) == 2
    for field in ("provider", "model", "tariff_id", "period", "reason"):
        assert saved[0][field] != saved[1][field]
        assert all(len(row[field]) <= 128 for row in saved)


def test_schema_contains_no_key_user_or_message_data_and_repeated_calls_share_bucket(
    store,
):
    record(store)
    original_size = store.status()["database_bytes"]
    for _ in range(100):
        record(store)
    assert len(rows(store)) == 1
    assert store.status()["database_bytes"] == original_size
    with closing(sqlite3.connect(store.path)) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(buckets)")}
    assert tables == {"buckets", "metadata"}
    assert (
        not {"key", "api_key", "user", "user_id", "message", "content", "call_id"}
        & columns
    )
    with pytest.raises(TypeError):
        record(store, api_key="do-not-store-this-key")
    assert b"do-not-store-this-key" not in store.path.read_bytes()


def test_capacity_failure_is_atomic_and_durable_without_deleting_old_archive(tmp_path):
    store = ConsumptionStore(tmp_path / "small.sqlite3", max_mib=0.03125)
    try:
        record(store)
        for index in range(1000):
            before = store.summary("qq", "bot-a")
            try:
                record(
                    store,
                    provider="p" * 100,
                    model=f"{index}" + "m" * 100,
                    tariff_id="t" * 100,
                    reason="r" * 100,
                )
            except StorageCapacityError:
                break
        else:
            pytest.fail("did not reach the configured SQLite limit")
        assert store.summary("qq", "bot-a") == before
        assert any(row["model"] == "deepseek-chat" for row in rows(store))
        status = store.status()
        assert status["write_failures"] == 1 and status["capacity_exceeded"]
        assert status["database_bytes"] <= status["max_bytes"]
        assert status["journal_bytes"] == 0
        store.close()
        store = ConsumptionStore(store.path, max_mib=0.03125)
        assert store.summary("qq", "bot-a") == before
        assert (
            store.status()["capacity_exceeded"]
            and store.status()["write_failures"] == 1
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("group", ""),
        ("group", " "),
        ("platform", ""),
        ("bot", "x" * 257),
        ("provider", ""),
        ("model", None),
        ("reason", 5),
        ("failed", 1),
        ("started_at", datetime(2026, 10, 29)),
        ("input_tokens", -1),
        ("cached_tokens", 101),
        ("output_tokens", True),
        ("total_tokens", 1 << 63),
        ("cost_cny", 0.1),
        ("cost_cny", Decimal("NaN")),
        ("cost_usd", Decimal("Infinity")),
        ("cost_usd", Decimal("-0.1")),
        ("cost_cny", Decimal("1e31")),
        ("cost_cny", Decimal("1e-31")),
    ],
)
def test_invalid_record_never_creates_a_bucket(store, field, value):
    with pytest.raises(ValueError):
        record(store, **{field: value})
    assert rows(store) == []
    assert store.status()["write_failures"] == 0


@pytest.mark.parametrize("day", ["20261029", "2026-1-1", "2026-02-30", "today", 123])
def test_archive_date_requires_exact_iso_date(store, day):
    with pytest.raises(ValueError):
        store.summary("qq", "bot-a", day=day)


def test_close_is_idempotent_and_operations_fail_explicitly(store):
    started = store.status()["started_at"]
    store.close()
    store.close()
    assert store.status()["closed"] and store.status()["started_at"] == started
    with pytest.raises(StorageError, match="closed"):
        record(store)
    with pytest.raises(StorageError, match="closed"):
        store.summary("qq", "bot-a")
