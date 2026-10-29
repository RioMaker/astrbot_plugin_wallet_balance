"""Conservative DeepSeek estimates from reported usage, never from message text.

Verified snapshot: 2026-10-29; prices are per million tokens.
Sources:
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
https://api-docs.deepseek.com/quick_start/pricing/
https://api-docs.deepseek.com/zh-cn/updates/
https://www.gov.cn/zhengce/content/202511/content_7047090.htm

The official documentation does not specify the billing instant at a peak/off-peak
boundary. This estimator explicitly uses the observed request start in Beijing
time. It does not claim to reproduce the provider's final bill.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any
from urllib.parse import urlsplit

TARIFF_ID = "deepseek-2026-10-29"
BEIJING = timezone(timedelta(hours=8))
VERIFIED_FROM = datetime(2026, 10, 29, tzinfo=BEIJING)
MAX_TOKENS = (1 << 63) - 1
_MISSING = object()

# Inclusive official public holiday intervals, including the whole rest period.
# Compensating workdays on Saturdays/Sundays remain off-peak under DeepSeek's
# explicit weekend rule, so no replacement-workday table is used here.
_HOLIDAYS_2026 = (
    (date(2026, 1, 1), date(2026, 1, 3)),
    (date(2026, 2, 15), date(2026, 2, 23)),
    (date(2026, 4, 4), date(2026, 4, 6)),
    (date(2026, 5, 1), date(2026, 5, 5)),
    (date(2026, 6, 19), date(2026, 6, 21)),
    (date(2026, 9, 25), date(2026, 9, 27)),
    (date(2026, 10, 1), date(2026, 10, 7)),
)
_MODELS = {
    "deepseek-flash": "flash",
    "deepseek-v4-flash": "flash",
    "deepseek-v4-flash-vision-exp": "flash",
    "deepseek-v4.1-flash-1017": "flash",
    "deepseek-v4-pro": "pro",
    "deepseek-v4-pro-0813": "pro",
}
# Order: cached input, uncached input, output. Peak rates are exactly twice these.
_RATES = {
    "flash": {
        "cny": tuple(map(Decimal, ("0.02", "1", "4"))),
        "usd": tuple(map(Decimal, ("0.003", "0.15", "0.6"))),
    },
    "pro": {
        "cny": tuple(map(Decimal, ("0.15", "4.5", "13.5"))),
        "usd": tuple(map(Decimal, ("0.022", "0.66", "1.98"))),
    },
}
_OFFICIAL_PATHS = {
    "",
    "/v1",
    "/beta",
    "/anthropic",
    "/chat/completions",
    "/v1/chat/completions",
    "/beta/chat/completions",
    "/beta/completions",
    "/responses",
    "/anthropic/v1/messages",
}


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class Quote:
    cost_cny: Decimal | None
    cost_usd: Decimal | None
    tariff_id: str
    period: str
    reason: str


def _field(obj: Any, name: str, default: Any = _MISSING) -> Any:
    try:
        return (
            obj.get(name, default)
            if isinstance(obj, Mapping)
            else getattr(obj, name, default)
        )
    except Exception:
        return default


def _valid_counter(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_TOKENS


def _sum(first: int | None, second: int | None) -> int | None:
    if first is None or second is None:
        return None
    total = first + second
    return total if total <= MAX_TOKENS else None


def _reported(obj: Any, *names: str) -> tuple[int | None, bool]:
    """Return a counter and whether a provided field was invalid/conflicting."""
    values = [_field(obj, name) for name in names]
    values = [value for value in values if value is not _MISSING and value is not None]
    if not values:
        return None, False
    if any(not _valid_counter(value) for value in values) or len(set(values)) != 1:
        return None, True
    return values[0], False


def _total(usage: Any, input_: int | None, output: int | None) -> int | None:
    reported, invalid = _reported(usage, "total_tokens")
    derived = _sum(input_, output)
    if invalid:
        return None
    if reported is None:
        return derived
    if input_ is not None and output is not None and reported != derived:
        return None
    return reported


def _standard_usage(usage: Any) -> Usage:
    if any(
        _field(usage, key) is not _MISSING
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    ):
        return _anthropic_usage(usage)
    input_, bad_input = _reported(usage, "prompt_tokens", "input_tokens")
    output, _ = _reported(usage, "completion_tokens", "output_tokens")
    hit, bad_hit = _reported(usage, "prompt_cache_hit_tokens")
    miss, bad_miss = _reported(usage, "prompt_cache_miss_tokens")
    prompt_detail, bad_prompt_detail = _reported(
        _field(usage, "prompt_tokens_details"), "cached_tokens"
    )
    input_detail, bad_input_detail = _reported(
        _field(usage, "input_tokens_details"), "cached_tokens"
    )
    reported_cache = [
        value for value in (hit, prompt_detail, input_detail) if value is not None
    ]
    bad_cache = (
        bad_hit or bad_prompt_detail or bad_input_detail or len(set(reported_cache)) > 1
    )
    cached = reported_cache[0] if reported_cache and not bad_cache else None
    if input_ is None and not bad_input and not bad_cache and not bad_miss:
        input_ = _sum(cached, miss)
    if (
        cached is None
        and not bad_cache
        and not bad_miss
        and input_ is not None
        and miss is not None
    ):
        cached = input_ - miss if miss <= input_ else None
    if bad_miss or (cached is not None and input_ is not None and cached > input_):
        cached = None
    if (
        input_ is not None
        and cached is not None
        and miss is not None
        and _sum(cached, miss) != input_
    ):
        cached = None
    return Usage(input_, cached, output, _total(usage, input_, output))


def _anthropic_usage(usage: Any) -> Usage:
    """Anthropic's base input excludes both cache reads and cache creation.

    Both cache counters must actually be reported, even when zero. Treat missing
    counters as unknown rather than importing an OpenAI-style inclusive input or
    a framework's default zero. Cache creation is charged as uncached input.
    """
    base, _ = _reported(usage, "input_tokens")
    cached, _ = _reported(usage, "cache_read_input_tokens")
    created, _ = _reported(usage, "cache_creation_input_tokens")
    output, _ = _reported(usage, "output_tokens")
    input_ = _sum(_sum(base, cached), created)
    return Usage(input_, cached, output, _total(usage, input_, output))


def _core_usage(usage: Any) -> Usage:
    other, _ = _reported(usage, "input_other")
    cached, _ = _reported(usage, "input_cached")
    output, _ = _reported(usage, "output")
    input_ = _sum(other, cached)
    return Usage(input_, cached, output, _total(usage, input_, output))


def normalize_usage(response: Any) -> Usage:
    """Read actual usage from dicts or SDK objects, preserving unknown fields.

    Explicit raw usage is authoritative, including incomplete/invalid usage; we
    never fill its missing cache count with a framework default. If raw usage is
    absent, use the framework usage. An explicit null raw usage plus all-zero
    framework counters is AstrBot's missing-usage sentinel, not a measured zero.
    Reasoning tokens are already part of completion/output tokens and are not
    added again. Invalid upstream objects never escape into the chat pipeline.
    """
    try:
        raw = _field(response, "raw_completion")
        raw_usage = _field(raw, "usage")
        if raw_usage is not _MISSING and raw_usage is not None:
            return _standard_usage(raw_usage)
        core = _field(response, "usage")
        if core is _MISSING or core is None:
            return Usage()
        if any(
            _field(core, key) is not _MISSING
            for key in ("input_other", "input_cached", "output")
        ):
            result = _core_usage(core)
        else:
            result = _standard_usage(core)
        if raw_usage is None and result == Usage(0, 0, 0, 0):
            return Usage()
        return result
    except Exception:
        return Usage()


def _official_endpoint(api_base: Any) -> bool:
    if not isinstance(api_base, str) or len(api_base) > 256:
        return False
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in api_base):
        return False
    if any(char in api_base for char in ("?", "#", "\\")):
        return False
    try:
        parts = urlsplit(api_base)
        path = parts.path[:-1] if parts.path.endswith("/") else parts.path
        return (
            parts.scheme == "https"
            and parts.hostname == "api.deepseek.com"
            and parts.netloc.lower() in ("api.deepseek.com", "api.deepseek.com:443")
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
            and path in _OFFICIAL_PATHS
        )
    except (TypeError, ValueError):
        return False


def _period(beijing_start: datetime) -> str:
    if beijing_start.year != 2026:
        return "unknown"
    day = beijing_start.date()
    holiday = any(first <= day <= last for first, last in _HOLIDAYS_2026)
    if beijing_start.weekday() >= 5 or holiday:
        return "off_peak"
    hour = beijing_start.hour
    return "peak" if 9 <= hour < 12 or 14 <= hour < 18 else "off_peak"


def _unpriced(reason: str, period: str = "unknown") -> Quote:
    return Quote(None, None, TARIFF_ID, period, reason)


def quote_usage(usage: Usage, model: Any, api_base: Any, started_at: Any) -> Quote:
    """Estimate CNY and USD independently, with fixed safe unpriced reasons.

    ``started_at`` must be the timezone-aware datetime captured before the real
    Provider call. It is converted to Beijing time without using host timezone.
    Missing cache usage, unknown calendars/models/endpoints, inconsistent counts
    and dates before this verified snapshot remain unpriced rather than guessed.
    """
    try:
        if not _official_endpoint(api_base):
            return _unpriced("非官方接口")
        family = _MODELS.get(model.strip().lower()) if isinstance(model, str) else None
        if family is None:
            return _unpriced("未知模型")
        if not isinstance(started_at, datetime) or started_at.utcoffset() is None:
            return _unpriced("时点无效")
        beijing_start = started_at.astimezone(BEIJING)
        period = _period(beijing_start)
        if beijing_start < VERIFIED_FROM:
            return _unpriced("早于价表验证日期", period)
        if period == "unknown":
            return _unpriced("节假日日历未知")

        counts = [
            _field(usage, key, None)
            for key in (
                "input_tokens",
                "cached_tokens",
                "output_tokens",
                "total_tokens",
            )
        ]
        if any(value is not None and not _valid_counter(value) for value in counts):
            return _unpriced("用量数据异常", period)
        input_, cached, output, total = counts
        if input_ is None or output is None or total is None:
            return _unpriced("用量不完整", period)
        if cached is None:
            return _unpriced("缺少缓存用量", period)
        if cached > input_ or _sum(input_, output) != total:
            return _unpriced("用量数据异常", period)
        with localcontext() as context:
            context.prec = 50
            factor = Decimal(2) if period == "peak" else Decimal(1)
            costs = []
            for currency in ("cny", "usd"):
                hit_rate, miss_rate, output_rate = _RATES[family][currency]
                costs.append(
                    (
                        Decimal(cached) * hit_rate
                        + Decimal(input_ - cached) * miss_rate
                        + Decimal(output) * output_rate
                    )
                    * factor
                    / Decimal(1_000_000)
                )
        return Quote(costs[0], costs[1], TARIFF_ID, period, "")
    except Exception:
        return _unpriced("用量数据异常")
