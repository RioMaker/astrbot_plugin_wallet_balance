"""Text reports use recorded values and expose unknown or incomplete coverage."""

import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from consumption_reports import group_usage_line, summary_text  # noqa: E402


def totals(**changes):
    return {
        "calls": 2,
        "failed_calls": 0,
        "input_tokens": 100,
        "cached_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 120,
        "missing_usage": 0,
        "unpriced_calls": 0,
        "cost_cny": Decimal("0.0864"),
        "cost_usd": Decimal("0.0123"),
        **changes,
    }


def summary(**changes):
    return {
        "started_at": "2026-10-29T15:59:59+00:00",
        "platform": "qq",
        "bot": "bot-a",
        "day": None,
        "groups": 1,
        "page": 1,
        "pages": 1,
        "totals": totals(),
        "entries": [{"platform": "qq", "bot": "bot-a", "group": "123", **totals()}],
        **changes,
    }


@pytest.mark.parametrize(
    "currency,expected",
    [
        ("CNY", "本群今日估算 ¥ 0.0864"),
        ("USD", "本群今日估算 0.0123 USD"),
    ],
)
def test_group_line_formats_currency_and_explicit_estimate(currency, expected):
    assert group_usage_line(summary(), currency) == expected


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("0", "0.0000"),
        ("0.000000001", "<0.0001"),
        ("0.0001", "0.0001"),
        ("1.23445", "1.2344"),
        ("1.23455", "1.2346"),
    ],
)
def test_small_nonzero_amounts_never_appear_as_zero_and_rounding_is_half_even(
    amount, expected
):
    data = summary(totals=totals(cost_cny=Decimal(amount)))
    with localcontext() as context:
        context.prec = 2
        assert group_usage_line(data, "CNY") == f"本群今日估算 ¥ {expected}"


def test_unpriced_calls_show_real_tokens_without_fabricated_zero_price():
    data = summary(
        totals=totals(unpriced_calls=2, cost_cny=Decimal(0), cost_usd=Decimal(0))
    )
    assert group_usage_line(data, "CNY") == "本群今日 120 Token · 未估价"
    assert "¥ 0" not in group_usage_line(data, "CNY")


def test_unknown_usage_never_claims_zero_tokens():
    values = totals(
        total_tokens=0, missing_usage=2, unpriced_calls=2, cost_cny=Decimal(0)
    )
    data = summary(totals=values)
    line = group_usage_line(data, "CNY")
    assert line == "本群今日 Token 未知 · 未估价"
    text = summary_text(data, period_label="累计")
    assert "本期合计：1 群 · Token 未知 · 未估价" in text
    assert "usage 缺失 2 次" in text and "未估价 2 次" in text


def test_partial_prices_missing_usage_and_record_failures_are_marked():
    partial = summary(totals=totals(unpriced_calls=1))
    assert (
        group_usage_line(partial, "CNY", incomplete=True)
        == "本群今日估算 ¥ 0.0864 · 部分未计价 · 记录不完整"
    )
    missing = summary(totals=totals(missing_usage=1))
    assert group_usage_line(missing, "CNY").endswith(" · Token缺失")
    assert group_usage_line(summary(), "CNY", ready=False) == "群消耗统计未就绪"
    assert (
        group_usage_line(summary(), "CNY", ready=False, incomplete=True)
        == "群消耗统计未就绪 · 记录不完整"
    )


def test_known_currency_cost_is_retained_when_other_currency_is_unpriced():
    data = summary(totals=totals(unpriced_calls=2, cost_usd=Decimal(0)))
    assert group_usage_line(data, "CNY") == "本群今日估算 ¥ 0.0864 · 部分未计价"
    assert group_usage_line(data, "USD") == "本群今日 120 Token · 未估价"


def test_no_calls_and_missing_data_are_distinct():
    no_calls = summary(totals=totals(calls=0, total_tokens=0, cost_cny=Decimal(0)))
    assert group_usage_line(no_calls, "CNY") == "本群今日暂无调用"
    assert group_usage_line({}, "CNY") == "本群今日统计未知"
    text = summary_text({}, period_label="累计")
    assert "统计开始：未知" in text and "Token 未知" in text and "费用未知" in text
    assert "0 Token" not in text and "¥ 0" not in text


@pytest.mark.parametrize(
    "value", [None, 0.1, Decimal("NaN"), Decimal("sNaN"), Decimal("-1")]
)
def test_invalid_or_absent_cost_is_unknown_not_zero(value):
    line = group_usage_line(summary(totals=totals(cost_cny=value)), "CNY")
    assert line == "本群今日 120 Token · 费用未知"


def test_admin_report_has_start_period_page_and_all_groups_totals():
    data = summary(
        groups=12,
        page=2,
        pages=3,
        totals=totals(total_tokens=9999, cost_cny=Decimal("10")),
    )
    text = summary_text(data, period_label="累计")
    assert "群 Token 与费用统计（估算）" in text
    assert "统计开始：2026-10-29 23:59:59 北京时间" in text
    assert "期间：累计" in text
    assert "本期合计：12 群 · 9,999 Token · 估算 ¥ 10.0000" in text
    assert "群 123：120 Token · 估算 ¥ 0.0864" in text
    assert "qq/bot-a" not in text
    assert text.endswith("第 2/3 页")


def test_admin_today_total_uses_separate_whole_scope_summary():
    data = summary(totals=totals(total_tokens=5000, cost_cny=Decimal("5")))
    today = summary(
        groups=3,
        totals=totals(total_tokens=99, cost_cny=Decimal("0.1"), failed_calls=1),
    )
    text = summary_text(data, period_label="累计", today=today)
    assert "今日合计：3 群 · 99 Token · 估算 ¥ 0.1000" in text
    assert "本期合计：1 群 · 5,000 Token · 估算 ¥ 5.0000" in text
    assert "今日提示：调用失败 1 次" in text
    assert text.index("今日合计") < text.index("本期合计")


def test_admin_report_marks_partial_usage_unpriced_failed_and_incomplete():
    values = totals(missing_usage=1, unpriced_calls=1, failed_calls=1)
    data = summary(totals=values, entries=[{"group": "123", **values}])
    text = summary_text(data, period_label="2026-10-29", ready=False, incomplete=True)
    assert "统计状态：未就绪" in text and "记录不完整" in text
    assert "120 Token（已知）" in text
    assert "usage 缺失 1 次；未估价 1 次；调用失败 1 次" in text


def test_all_scope_labels_distinguish_identical_group_ids():
    data = summary(
        platform=None,
        bot=None,
        groups=2,
        entries=[
            {"platform": "qq", "bot": "bot-a", "group": "123", **totals()},
            {"platform": "wechat", "bot": "bot-b", "group": "123", **totals()},
        ],
    )
    text = summary_text(data, period_label="累计")
    assert "qq/bot-a · 群 123：" in text
    assert "wechat/bot-b · 群 123：" in text


def test_group_labels_are_control_free_and_bounded():
    group = "a" * 100 + "\n\x00\u202e伪造条目"
    data = summary(entries=[{"group": group, **totals()}])
    text = summary_text(data, period_label="累计\n\u202e")
    assert "\x00" not in text and "\u202e" not in text and "伪造条目" not in text
    label = next(line for line in text.splitlines() if line.startswith("群 a")).split(
        "：", 1
    )[0][2:]
    assert len(label) == 64 and label.endswith("…")
    assert "期间：累计" in text


@pytest.mark.parametrize("started", [None, "bad", "2026-10-29T12:00:00"])
def test_bad_or_naive_start_time_is_not_assigned_an_invented_timezone(started):
    assert "统计开始：未知" in summary_text(
        summary(started_at=started), period_label="累计"
    )


def test_empty_archive_has_no_call_message_and_known_zero_totals():
    values = totals(calls=0, total_tokens=0, cost_cny=Decimal(0))
    text = summary_text(
        summary(groups=0, entries=[], totals=values), period_label="今天"
    )
    assert "暂无群调用记录。" in text
    assert "本期合计：0 群 · 0 Token · 估算 ¥ 0.0000" in text


def test_unsupported_currency_is_not_assumed_to_be_cny():
    with pytest.raises(ValueError):
        group_usage_line(summary(), "EUR")
