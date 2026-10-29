from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from types import SimpleNamespace as NS

import pytest

import pricing
from pricing import BEIJING, MAX_TOKENS, Usage, normalize_usage, quote_usage

OFFICIAL = "https://api.deepseek.com"
START = datetime(2026, 10, 29, 10, tzinfo=BEIJING)
COMPLETE = Usage(100, 20, 10, 110)


def quote(usage=COMPLETE, **overrides):
    return quote_usage(
        usage,
        overrides.get("model", "deepseek-flash"),
        overrides.get("api_base", OFFICIAL),
        overrides.get("started_at", START),
    )


@pytest.mark.parametrize("wrapper", [dict, NS])
def test_raw_usage_wins_and_reasoning_is_not_double_counted(wrapper):
    response = wrapper(
        raw_completion=wrapper(
            usage=wrapper(
                prompt_tokens=100,
                prompt_cache_hit_tokens=20,
                prompt_cache_miss_tokens=80,
                completion_tokens=10,
                total_tokens=110,
                completion_tokens_details=wrapper(reasoning_tokens=7),
            )
        ),
        usage=wrapper(input_other=1000, input_cached=999, output=900),
    )
    assert normalize_usage(response) == COMPLETE


@pytest.mark.parametrize("details", ["prompt_tokens_details", "input_tokens_details"])
def test_cache_details_and_derived_total(details):
    response = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            details: {"cached_tokens": 20},
        }
    }
    assert normalize_usage(response) == COMPLETE


@pytest.mark.parametrize(
    "usage",
    [
        {
            "prompt_cache_hit_tokens": 20,
            "prompt_cache_miss_tokens": 80,
            "completion_tokens": 10,
        },
        {"prompt_tokens": 100, "prompt_cache_miss_tokens": 80, "completion_tokens": 10},
    ],
)
def test_reported_cache_split_can_derive_input_or_cached(usage):
    assert normalize_usage({"raw_completion": {"usage": usage}}) == COMPLETE


def test_core_fallback_and_explicit_missing_usage_zero_sentinel():
    zero = NS(input_other=0, input_cached=0, output=0)
    assert normalize_usage(NS(raw_completion=NS(usage=None), usage=zero)) == Usage()
    assert normalize_usage(NS(raw_completion=NS(), usage=zero)) == Usage(0, 0, 0, 0)
    assert (
        normalize_usage(NS(usage=NS(input_other=80, input_cached=20, output=10)))
        == COMPLETE
    )
    assert (
        normalize_usage(
            NS(
                raw_completion=NS(usage=None),
                usage=NS(input_other=80, input_cached=20, output=10),
            )
        )
        == COMPLETE
    )
    assert normalize_usage(
        {
            "raw_completion": {
                "usage": {
                    "prompt_tokens": 0,
                    "prompt_cache_hit_tokens": 0,
                    "completion_tokens": 0,
                }
            },
            "usage": zero,
        }
    ) == Usage(0, 0, 0, 0)
    assert quote(Usage(0, 0, 0, 0)).cost_cny == Decimal(0)


def test_raw_missing_cache_does_not_take_framework_zero():
    result = normalize_usage(
        {
            "raw_completion": {
                "usage": {"prompt_tokens": 100, "completion_tokens": 10}
            },
            "usage": NS(input_other=100, input_cached=0, output=10),
        }
    )
    assert result == Usage(100, None, 10, 110)
    assert quote(result).reason == "缺少缓存用量"
    assert (
        normalize_usage(
            {
                "raw_completion": {"usage": {}},
                "usage": NS(input_other=100, input_cached=0, output=10),
            }
        )
        == Usage()
    )


@pytest.mark.parametrize("value", [-1, True, 1.0, "1", MAX_TOKENS + 1, [], {}])
def test_invalid_upstream_counter_preserves_other_usage_but_does_not_price(value):
    result = normalize_usage(
        {
            "usage": {
                "prompt_tokens": value,
                "prompt_cache_hit_tokens": 0,
                "completion_tokens": 10,
            }
        }
    )
    assert result.input_tokens is None and result.output_tokens == 10
    assert quote(result).cost_cny is None


@pytest.mark.parametrize(
    "extra",
    [
        {"prompt_cache_miss_tokens": 81},
        {"prompt_cache_hit_tokens": 101},
        {"prompt_tokens_details": {"cached_tokens": 19}},
        {"prompt_cache_hit_tokens": "20"},
        {"prompt_cache_miss_tokens": False},
        {"total_tokens": 111},
        {"total_tokens": -1},
        {"input_tokens": 101},
        {"output_tokens": 9},
    ],
)
def test_inconsistent_fields_are_not_silently_repriced(extra):
    result = normalize_usage(
        {
            "raw_completion": {
                "usage": {
                    "prompt_tokens": 100,
                    "prompt_cache_hit_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    **extra,
                }
            }
        }
    )
    assert quote(result).cost_cny is None


def test_int64_sum_overflow_does_not_escape_normalization():
    assert (
        normalize_usage(
            {"usage": NS(input_other=MAX_TOKENS, input_cached=1, output=0)}
        ).input_tokens
        is None
    )
    result = normalize_usage(
        {"usage": NS(input_other=MAX_TOKENS, input_cached=0, output=1)}
    )
    assert result == Usage(MAX_TOKENS, 0, 1, None)
    assert quote(result).cost_cny is None


def test_partial_total_is_preserved_without_inventing_input():
    assert normalize_usage({"usage": {"total_tokens": 42}}) == Usage(
        None, None, None, 42
    )
    assert quote(normalize_usage(None)).reason == "用量不完整"


@pytest.mark.parametrize("wrapper", [dict, NS])
def test_anthropic_input_includes_cache_reads_and_creation_once(wrapper):
    response = wrapper(
        raw_completion=wrapper(
            usage=wrapper(
                input_tokens=70,
                cache_read_input_tokens=20,
                cache_creation_input_tokens=10,
                output_tokens=10,
                # These OpenAI-style fields must not override Anthropic semantics.
                prompt_tokens=70,
                input_tokens_details={"cached_tokens": 0},
                completion_tokens_details={"reasoning_tokens": 8},
            )
        ),
        usage=wrapper(input_other=999, input_cached=0, output=999),
    )
    result = normalize_usage(response)
    assert result == COMPLETE
    assert quote(result, api_base=OFFICIAL + "/anthropic").cost_cny == Decimal(
        "0.0002408"
    )


@pytest.mark.parametrize(
    "counters",
    [
        {"cache_read_input_tokens": 20},
        {"cache_creation_input_tokens": 10},
        {"cache_read_input_tokens": None, "cache_creation_input_tokens": 0},
        {"cache_read_input_tokens": 0, "cache_creation_input_tokens": None},
        {"cache_read_input_tokens": 0, "cache_creation_input_tokens": -1},
        {"cache_read_input_tokens": True, "cache_creation_input_tokens": 0},
        {"cache_read_input_tokens": MAX_TOKENS, "cache_creation_input_tokens": 0},
    ],
)
def test_anthropic_missing_invalid_or_overflowing_cache_is_unpriced(counters):
    result = normalize_usage(
        {
            "raw_completion": {
                "usage": {"input_tokens": 70, "output_tokens": 10, **counters}
            },
            "usage": NS(input_other=70, input_cached=0, output=10),
        }
    )
    assert result.input_tokens is None and result.output_tokens == 10
    assert quote(result, api_base=OFFICIAL + "/anthropic").cost_cny is None


def test_anthropic_explicit_zero_cache_is_priced_as_reported():
    result = normalize_usage(
        {
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 10,
            }
        }
    )
    assert result == Usage(100, 0, 10, 110)
    assert quote(result).cost_cny == Decimal("0.00028")


@pytest.mark.parametrize(
    ("hour", "minute", "second", "period"),
    [
        (8, 59, 59, "off_peak"),
        (9, 0, 0, "peak"),
        (11, 59, 59, "peak"),
        (12, 0, 0, "off_peak"),
        (13, 59, 59, "off_peak"),
        (14, 0, 0, "peak"),
        (17, 59, 59, "peak"),
        (18, 0, 0, "off_peak"),
        (23, 59, 59, "off_peak"),
    ],
)
def test_peak_boundaries_use_beijing_request_start(hour, minute, second, period):
    start = START.replace(hour=hour, minute=minute, second=second)
    result = quote(started_at=start.astimezone(timezone.utc))
    assert result.period == period and not result.reason


@pytest.mark.parametrize(
    ("model", "cny", "usd"),
    [
        ("deepseek-flash", "5.02", "0.753"),
        ("deepseek-v4-pro", "18.15", "2.662"),
    ],
)
def test_both_currency_rates_use_cache_and_decimal_arithmetic(model, cny, usd):
    usage = Usage(2_000_000, 1_000_000, 1_000_000, 3_000_000)
    off_peak = quote(usage, model=model, started_at=START.replace(hour=12))
    with localcontext() as context:
        context.prec = 2
        peak = quote(usage, model=model)
    assert off_peak.cost_cny == Decimal(cny)
    assert off_peak.cost_usd == Decimal(usd)
    assert peak.cost_cny == Decimal(cny) * 2
    assert peak.cost_usd == Decimal(usd) * 2
    assert peak.tariff_id == "deepseek-2026-10-29"


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "DeepSeek-V4.1-Flash-1017",
    ],
)
def test_documented_flash_aliases(model):
    assert quote(model=model) == quote(model="deepseek-flash")


def test_pro_continues_after_september_14_with_its_own_rate():
    assert quote(model="DeepSeek-V4-Pro-0813") == quote(model="deepseek-v4-pro")
    assert (
        quote(model="deepseek-v4-pro").cost_cny > quote(model="deepseek-flash").cost_cny
    )


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-chat",
        "deepseek-reasoner",
        "custom-deepseek-v4-pro",
        "deepseek-v4.1-flash",
        None,
        [],
    ],
)
def test_unknown_models_are_unpriced(model):
    assert quote(model=model).reason == "未知模型"


@pytest.mark.parametrize(
    "api_base",
    [
        "https://api.deepseek.com",
        "https://api.deepseek.com:443/",
        "https://api.deepseek.com/v1/",
        "https://api.deepseek.com/beta",
        "https://api.deepseek.com/anthropic",
        "https://api.deepseek.com/chat/completions",
        "https://api.deepseek.com/anthropic/v1/messages",
    ],
)
def test_official_known_endpoints(api_base):
    assert quote(api_base=api_base).cost_cny is not None


@pytest.mark.parametrize(
    "api_base",
    [
        "http://api.deepseek.com",
        "https://api.deepseek.com:444",
        "https://api.deepseek.com.evil.example",
        "https://relay.example/v1",
        "https://api.deepseek.com/user/balance",
        "https://api.deepseek.com/unknown",
        "https://api.deepseek.com/v1//",
        "https://api.deepseek.com/v1?",
        "https://api.deepseek.com/#",
        "https://key@api.deepseek.com/v1",
        "https://api.deepseek.com:bad",
        "https://api.deepseek.com:",
        "https://api.deepseek.com:0443",
        "\nhttps://api.deepseek.com",
        "https://api.deepseek.com/%76%31",
        "https://api.deepseek.com\\v1",
        None,
        {},
    ],
)
def test_relays_and_malformed_endpoints_never_receive_official_prices(api_base):
    result = quote(api_base=api_base)
    assert result.reason == "非官方接口" and result.cost_usd is None


@pytest.mark.parametrize(
    "day",
    [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
        "2026-02-15",
        "2026-02-23",
        "2026-04-04",
        "2026-04-06",
        "2026-05-01",
        "2026-05-05",
        "2026-06-19",
        "2026-06-21",
        "2026-09-25",
        "2026-09-27",
        "2026-10-01",
        "2026-10-07",
        # All replacement-workday weekends still use DeepSeek's weekend rate.
        "2026-01-04",
        "2026-02-14",
        "2026-02-28",
        "2026-05-09",
        "2026-09-20",
        "2026-10-10",
    ],
)
def test_2026_holiday_calendar_and_weekend_workdays(day):
    at_noon = datetime.fromisoformat(day).replace(hour=10, tzinfo=BEIJING)
    assert pricing._period(at_noon) == "off_peak"
    # Calendar knowledge does not authorize historical use of a new price snapshot.
    assert quote(started_at=at_noon).reason == "早于价表验证日期"


def test_current_weekend_and_snapshot_boundaries():
    assert quote(started_at=START.replace(day=31)).period == "off_peak"
    assert quote(started_at=pricing.VERIFIED_FROM).cost_cny is not None
    assert (
        quote(started_at=pricing.VERIFIED_FROM - timedelta(microseconds=1)).reason
        == "早于价表验证日期"
    )
    assert (
        quote(started_at=datetime(2027, 1, 2, 10, tzinfo=BEIJING)).reason
        == "节假日日历未知"
    )


@pytest.mark.parametrize(
    "start", [None, START.replace(tzinfo=None), "2026-10-29", 1793239200]
)
def test_invalid_start_time_never_uses_machine_timezone(start):
    assert quote(started_at=start).reason == "时点无效"


@pytest.mark.parametrize(
    "usage",
    [
        Usage(10, 11, 2, 12),
        Usage(10, 0, 2, 99),
        Usage(True, 0, 0, 1),
        Usage(MAX_TOKENS + 1, 0, 0, MAX_TOKENS + 1),
        Usage(10, 0, -1, 9),
    ],
)
def test_quote_rechecks_direct_dataclass_values(usage):
    assert quote(usage).reason == "用量数据异常"


def test_broken_upstream_objects_do_not_interrupt_chat():
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("secret upstream object failure")

    assert normalize_usage(Broken()) == Usage()
    assert quote(Broken()).cost_cny is None
    assert "secret" not in quote(Broken()).reason
