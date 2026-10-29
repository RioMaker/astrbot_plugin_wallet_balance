"""Plain text views of recorded group tokens and historical cost estimates."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

_BEIJING = timezone(timedelta(hours=8))


def _clean(value, *, limit: int = 64) -> str:
    if not isinstance(value, str):
        return "未知"
    text = "".join(
        char for char in value if not unicodedata.category(char).startswith("C")
    )
    text = " ".join(text.split())
    return (text[: limit - 1] + "…" if len(text) > limit else text) or "未知"


def _count(value) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _totals(summary) -> dict:
    if isinstance(summary, dict) and isinstance(summary.get("totals"), dict):
        return summary["totals"]
    return {}


def _amount(value) -> str | None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return None
    if value.adjusted() > 60 or len(value.as_tuple().digits) > 128:
        return None
    if 0 < value < Decimal("0.0001"):
        return "<0.0001"
    with localcontext() as context:
        context.prec = max(128, value.adjusted() + 6)
        return format(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN), "f")


def _tokens_text(values: dict) -> str:
    tokens = _count(values.get("total_tokens"))
    calls = _count(values.get("calls"))
    missing = _count(values.get("missing_usage"))
    if tokens is None or (
        tokens == 0 and calls and missing is not None and missing >= calls
    ):
        return "Token 未知"
    return f"{tokens:,} Token" + ("（已知）" if missing else "")


def _price_text(values: dict, currency: str) -> str:
    value = values.get("cost_cny" if currency == "CNY" else "cost_usd")
    amount = _amount(value)
    calls = _count(values.get("calls"))
    unpriced = _count(values.get("unpriced_calls"))
    if (
        calls
        and unpriced is not None
        and unpriced >= calls
        and (amount is None or value == 0)
    ):
        return "未估价"
    if amount is None:
        return "费用未知"
    money = f"¥ {amount}" if currency == "CNY" else f"{amount} USD"
    return f"估算 {money}"


def group_usage_line(
    summary: dict, currency: str, *, ready: bool = True, incomplete: bool = False
) -> str:
    """One group's recorded calls for today's supplied scope, never live billing."""
    if currency not in ("CNY", "USD"):
        raise ValueError("currency must be CNY or USD")
    if not ready:
        text = "群消耗统计未就绪"
    else:
        totals = _totals(summary)
        calls = _count(totals.get("calls"))
        if calls == 0:
            text = "本群今日暂无调用"
        elif calls is None:
            text = "本群今日统计未知"
        else:
            price = _price_text(totals, currency)
            if price.startswith("估算 "):
                text = "本群今日" + price
                if _count(totals.get("unpriced_calls")):
                    text += " · 部分未计价"
                elif _count(totals.get("missing_usage")):
                    text += " · Token缺失"
            else:
                text = f"本群今日 {_tokens_text(totals)} · {price}"
    return text + (" · 记录不完整" if incomplete else "")


def _started_at(value) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return "未知"
        return parsed.astimezone(_BEIJING).strftime("%Y-%m-%d %H:%M:%S 北京时间")
    except (AttributeError, TypeError, ValueError):
        return "未知"


def _notes(values: dict) -> str:
    parts = []
    for field, label in (
        ("missing_usage", "usage 缺失"),
        ("unpriced_calls", "未估价"),
        ("failed_calls", "调用失败"),
    ):
        count = _count(values.get(field))
        if count:
            parts.append(f"{label} {count:,} 次")
        elif count is None:
            parts.append(f"{label}次数未知")
    return "；".join(parts)


def _aggregate_line(summary: dict) -> str:
    groups = _count(summary.get("groups"))
    totals = _totals(summary)
    return (
        f"{groups if groups is not None else '未知'} 群 · "
        f"{_tokens_text(totals)} · {_price_text(totals, 'CNY')}"
    )


def summary_text(
    summary: dict,
    *,
    period_label: str,
    today: dict | None = None,
    ready: bool = True,
    incomplete: bool = False,
) -> str:
    """Administrator report of archived estimates for the caller-supplied scope."""
    summary = summary if isinstance(summary, dict) else {}
    totals = _totals(summary)
    lines = [
        "群 Token 与费用统计（估算）",
        "统计开始：" + _started_at(summary.get("started_at")),
        "期间：" + _clean(period_label),
    ]
    if not ready:
        lines.append("统计状态：未就绪，以下仅为已存档数据。")
    if incomplete:
        lines.append("注意：记录不完整，仅汇总成功存档的数据。")
    if today is not None:
        today = today if isinstance(today, dict) else {}
        lines.append("今日合计：" + _aggregate_line(today))
        notes = _notes(_totals(today))
        if notes:
            lines.append("今日提示：" + notes)
    lines.append("本期合计：" + _aggregate_line(summary))
    notes = _notes(totals)
    if notes:
        lines.append("本期提示：" + notes)
    entries = summary.get("entries")
    if isinstance(entries, list) and entries:
        broad_scope = summary.get("platform") is None or summary.get("bot") is None
        for entry in entries:
            if not isinstance(entry, dict):
                lines.append("群明细不可用。")
                continue
            label = "群 " + _clean(entry.get("group"))
            if broad_scope and ("platform" in entry or "bot" in entry):
                label = f"{_clean(entry.get('platform'))}/{_clean(entry.get('bot'))} · {label}"
            line = f"{label}：{_tokens_text(entry)} · {_price_text(entry, 'CNY')}"
            notes = _notes(entry)
            if notes:
                line += f"（{notes}）"
            lines.append(line)
    elif entries == [] and _count(totals.get("calls")) == 0:
        lines.append("暂无群调用记录。")
    else:
        lines.append("群明细不可用。")
    page, pages = _count(summary.get("page")), _count(summary.get("pages"))
    lines.append(f"第 {page or '未知'}/{pages or '未知'} 页")
    return "\n".join(lines)
