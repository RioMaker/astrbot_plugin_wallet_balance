"""Bounded SQLite archives of daily group usage; no messages or call logs."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Iterator

_BEIJING = timezone(timedelta(hours=8))
_MAX_INT = (1 << 63) - 1
_META_BYTES = 2048
_APP_ID = 0x57424C43
_COUNTERS = (
    "calls",
    "failed_calls",
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "total_tokens",
    "missing_usage",
    "unpriced_calls",
)
_COSTS = ("cost_cny", "cost_usd")
_KEYS = (
    "platform",
    "bot",
    "group_id",
    "day",
    "provider",
    "model",
    "tariff_id",
    "period",
    "reason",
)
_KEY_WHERE = " AND ".join(f"{key}=?" for key in _KEYS)


class StorageError(RuntimeError):
    """The consumption archive could not be read or updated."""


class StorageCapacityError(StorageError):
    """An atomic write exceeded the configured SQLite page limit."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
        raise ValueError(f"{label} must contain 1..256 characters")
    return value.strip()


def _label(value: str, label: str, *, optional: bool = False) -> str:
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise ValueError(
            f"{label} must be a string" + ("" if optional else " and nonempty")
        )
    value = value.strip()
    if len(value) <= 128:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return value[:103] + "~" + digest


def _token(value: int | None) -> int | None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_INT
    ):
        raise ValueError("token counts must be nonnegative signed 64-bit integers")
    return value


def _cost(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("cost must be a finite nonnegative Decimal or None")
    parts = value.as_tuple()
    if (
        len(parts.digits) > 80
        or not -30 <= parts.exponent <= 30
        or value.adjusted() > 30
    ):
        raise ValueError("cost exceeds supported precision or range")
    return value


def _add_cost(first: Decimal, second: Decimal) -> Decimal:
    # Each amount has at most 31 integer and 30 fractional places; at most INT64
    # calls fit in a bucket. 128 digits also cover aggregation of the bounded DB.
    with localcontext() as context:
        context.prec = 128
        return first + second


def _zero() -> dict:
    return {**dict.fromkeys(_COUNTERS, 0), **dict.fromkeys(_COSTS, Decimal(0))}


class ConsumptionStore:
    """Thread-safe, persistent daily buckets with an atomic hard page limit.

    Costs are the caller's historical estimates, stored exactly as decimal text.
    Neither reads nor subsequent writes reprice earlier calls. DELETE journals
    may temporarily occupy additional disk space during a transaction.
    """

    def __init__(self, path: str | Path, max_mib: float = 64):
        if (
            isinstance(max_mib, bool)
            or not isinstance(max_mib, (int, float))
            or not math.isfinite(max_mib)
            or max_mib <= 0
        ):
            raise ValueError("max_mib must be a positive finite number")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._state = {
            "started_at": None,
            "updated_at": None,
            "write_failures": 0,
            "last_error": None,
            "capacity_exceeded": False,
        }
        self._conn = sqlite3.connect(
            str(self.path), timeout=5, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
            self._max_pages = int(max_mib * 1024 * 1024) // self._page_size
            if self._max_pages < 8:
                raise ValueError("max_mib must allow at least 8 SQLite pages")
            if self._conn.execute("PRAGMA page_count").fetchone()[0] > self._max_pages:
                raise StorageCapacityError(
                    "existing consumption archive exceeds the configured limit"
                )
            actual = self._conn.execute(
                f"PRAGMA max_page_count={self._max_pages}"
            ).fetchone()[0]
            if actual != self._max_pages:
                raise StorageCapacityError("could not apply the database page limit")
            self._initialize()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def _initialize(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        application = self._conn.execute("PRAGMA application_id").fetchone()[0]
        if version not in (0, 1) or application not in (0, _APP_ID):
            raise StorageError("unsupported consumption archive schema")
        if version == 1 and application != _APP_ID:
            raise StorageError("the database is not a consumption archive")
        with self._transaction(write=True):
            self._conn.execute("""CREATE TABLE IF NOT EXISTS buckets (
                platform TEXT NOT NULL, bot TEXT NOT NULL, group_id TEXT NOT NULL,
                day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                tariff_id TEXT NOT NULL, period TEXT NOT NULL, reason TEXT NOT NULL,
                calls INTEGER NOT NULL, failed_calls INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                missing_usage INTEGER NOT NULL, unpriced_calls INTEGER NOT NULL,
                cost_cny TEXT NOT NULL, cost_usd TEXT NOT NULL,
                PRIMARY KEY(platform, bot, group_id, day, provider, model,
                            tariff_id, period, reason)
            ) WITHOUT ROWID""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS metadata (
                id INTEGER PRIMARY KEY CHECK(id=1),
                state BLOB NOT NULL CHECK(length(state)=2048)
            )""")
            state = {**self._state, "started_at": _now()}
            self._conn.execute(
                "INSERT OR IGNORE INTO metadata(id,state) VALUES(1,?)",
                (self._encode_metadata(state),),
            )
            self._conn.execute(f"PRAGMA application_id={_APP_ID}")
            self._conn.execute("PRAGMA user_version=1")
        self._state = self._metadata()

    @staticmethod
    def _encode_metadata(state: dict) -> bytes:
        value = json.dumps(state, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        if len(value) > _META_BYTES:
            raise StorageError("consumption metadata exceeds its reserved space")
        return value.ljust(_META_BYTES, b" ")

    def _metadata(self) -> dict:
        return json.loads(
            self._conn.execute("SELECT state FROM metadata WHERE id=1").fetchone()[0]
        )

    def _save_metadata(self, state: dict) -> None:
        self._conn.execute(
            "UPDATE metadata SET state=? WHERE id=1", (self._encode_metadata(state),)
        )

    def _record_failure(self, exc: Exception, full: bool) -> None:
        state = dict(self._state)
        state.update(
            write_failures=state["write_failures"] + 1,
            last_error=str(exc)[:256],
            capacity_exceeded=full,
        )
        self._state = state
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            saved = self._metadata()
            state["write_failures"] = max(
                state["write_failures"], saved["write_failures"] + 1
            )
            state["started_at"] = saved["started_at"]
            state["updated_at"] = saved["updated_at"]
            self._save_metadata(state)
            self._conn.commit()
        except (sqlite3.Error, StorageError, ValueError, TypeError):
            self._conn.rollback()

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        with self._lock:
            if self._closed:
                raise StorageError("consumption archive is closed")
            try:
                self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield
                self._conn.commit()
            except BaseException as exc:
                self._conn.rollback()
                full = (
                    getattr(exc, "sqlite_errorcode", None) == 13
                    or "database or disk is full" in str(exc).lower()
                )
                if write and isinstance(exc, Exception):
                    self._record_failure(exc, full)
                if isinstance(exc, sqlite3.Error):
                    error_type = StorageCapacityError if full else StorageError
                    raise error_type(str(exc)) from exc
                raise

    def record(
        self,
        *,
        platform: str,
        bot: str,
        group: str,
        provider: str,
        model: str,
        started_at: datetime,
        input_tokens: int | None,
        cached_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        cost_cny: Decimal | None,
        cost_usd: Decimal | None,
        tariff_id: str,
        period: str,
        reason: str = "",
        failed: bool = False,
    ) -> None:
        platform, bot, group = (
            _identity(platform, "platform"),
            _identity(bot, "bot"),
            _identity(group, "group"),
        )
        if not isinstance(started_at, datetime) or started_at.utcoffset() is None:
            raise ValueError("started_at must be a timezone-aware datetime")
        if not isinstance(failed, bool):
            raise ValueError("failed must be a boolean")
        input_tokens, cached_tokens, output_tokens, total_tokens = (
            _token(value)
            for value in (input_tokens, cached_tokens, output_tokens, total_tokens)
        )
        if (
            input_tokens is not None
            and cached_tokens is not None
            and cached_tokens > input_tokens
        ):
            raise ValueError("cached_tokens cannot exceed input_tokens")
        cost_cny, cost_usd = _cost(cost_cny), _cost(cost_usd)
        key = (
            platform,
            bot,
            group,
            started_at.astimezone(_BEIJING).date().isoformat(),
            _label(provider, "provider"),
            _label(model, "model"),
            _label(tariff_id, "tariff_id", optional=True),
            _label(period, "period", optional=True),
            _label(reason, "reason", optional=True),
        )
        counters = (
            1,
            int(failed),
            input_tokens or 0,
            cached_tokens or 0,
            output_tokens or 0,
            total_tokens or 0,
            int(total_tokens is None),
            int(cost_cny is None or cost_usd is None),
        )
        costs = (cost_cny or Decimal(0), cost_usd or Decimal(0))
        with self._transaction(write=True):
            row = self._conn.execute(
                f"SELECT * FROM buckets WHERE {_KEY_WHERE}", key
            ).fetchone()
            if row is not None:
                counters = tuple(
                    row[field] + value for field, value in zip(_COUNTERS, counters)
                )
                if any(value > _MAX_INT for value in counters):
                    raise StorageError(
                        "consumption counter exceeds the supported integer range"
                    )
                costs = tuple(
                    _add_cost(Decimal(row[field]), value)
                    for field, value in zip(_COSTS, costs)
                )
            fields = (*_KEYS, *_COUNTERS, *_COSTS)
            self._conn.execute(
                f"INSERT OR REPLACE INTO buckets({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                (*key, *counters, *(format(value, "f") for value in costs)),
            )
            state = self._metadata()
            state.update(updated_at=_now(), capacity_exceeded=False)
            self._save_metadata(state)
            self._state = state

    def summary(
        self,
        platform: str | None,
        bot: str | None,
        *,
        group: str | None = None,
        day: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict:
        parameters = []
        conditions = []
        if platform is not None:
            platform = _identity(platform, "platform")
            parameters.append(platform)
            conditions.append("platform=?")
        if bot is not None:
            bot = _identity(bot, "bot")
            parameters.append(bot)
            conditions.append("bot=?")
        if group is not None:
            parameters.append(_identity(group, "group"))
            conditions.append("group_id=?")
        if day is not None:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError("day must be an ISO YYYY-MM-DD date")
            parameters.append(day)
            conditions.append("day=?")
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        groups = {}
        totals = _zero()
        with self._transaction(write=False):
            started_at = self._metadata()["started_at"]
            rows = self._conn.execute(
                "SELECT platform,bot,group_id,"
                + ",".join((*_COUNTERS, *_COSTS))
                + " FROM buckets"
                + (" WHERE " + " AND ".join(conditions) if conditions else ""),
                parameters,
            )
            for row in rows:
                entry = groups.setdefault(
                    (row["platform"], row["bot"], row["group_id"]),
                    {
                        "platform": row["platform"],
                        "bot": row["bot"],
                        "group": row["group_id"],
                        **_zero(),
                    },
                )
                for field in _COUNTERS:
                    entry[field] += row[field]
                    totals[field] += row[field]
                for field in _COSTS:
                    value = Decimal(row[field])
                    entry[field] = _add_cost(entry[field], value)
                    totals[field] = _add_cost(totals[field], value)
        entries = sorted(
            groups.values(),
            key=lambda item: (item["group"], item["platform"], item["bot"]),
        )
        entries.sort(key=lambda item: item["cost_cny"], reverse=True)
        pages = max(1, (len(entries) + page_size - 1) // page_size)
        page = min(page, pages)
        return {
            "started_at": started_at,
            "platform": platform,
            "bot": bot,
            "day": day,
            "entries": entries[(page - 1) * page_size : page * page_size],
            "totals": totals,
            "groups": len(entries),
            "page": page,
            "pages": pages,
        }

    def status(self) -> dict:
        with self._lock:
            pages = free = 0
            if not self._closed:
                try:
                    saved = self._metadata()
                    if saved["write_failures"] >= self._state["write_failures"]:
                        self._state = saved
                    pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
                    free = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
                except sqlite3.Error as exc:
                    self._state["last_error"] = str(exc)[:256]
            database_bytes = self.path.stat().st_size if self.path.exists() else 0
            journal_bytes = sum(
                extra.stat().st_size
                for suffix in ("-journal", "-wal", "-shm")
                if (extra := Path(str(self.path) + suffix)).exists()
            )
            return {
                **self._state,
                "database_bytes": database_bytes,
                "journal_bytes": journal_bytes,
                "total_bytes": database_bytes + journal_bytes,
                "max_bytes": self._max_pages * self._page_size,
                "page_count": pages,
                "free_pages": free,
                "closed": self._closed,
            }

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
