"""Draw a current wallet balance inside the supplied, unmodified bubble artwork.

Only text inside TEXT_BOUNDS is painted. The caller supplies the balance and its
original query time; this module neither queries an API nor refreshes a cache.
"""

from __future__ import annotations

import io
import os
import unicodedata
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BACKGROUND_PATH = Path(__file__).resolve().parent / "assets" / "balance-bubble.jpg"
CANVAS_SIZE = (1254, 1254)
TEXT_BOUNDS = (130, 140, 640, 450)
CENTER_X = 385
INK = "#102F5F"
TITLE_INK = "#536BA9"
SECONDARY_INK = "#7686A8"
GROUP_USAGE_INK = "#9C6D1E"
UNAVAILABLE_INK = "#B53C4A"
BALANCE_GRADIENT = ("#203170", "#344C92", "#4A66B4", "#647ED2", "#8284E0")


class BalanceRenderError(ValueError):
    """The caller can fall back to its full text response on this error."""


class ChineseFontUnavailable(BalanceRenderError):
    """No readable Chinese font was found."""


def _font_candidates(font_path: str | Path | None) -> list[Path]:
    if font_path:
        return [Path(font_path).expanduser()]
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return [
        windows_fonts / "msyh.ttc",
        windows_fonts / "msyhbd.ttc",
        windows_fonts / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]


def resolve_font_path(font_path: str | Path | None = None) -> Path:
    for candidate in _font_candidates(font_path):
        if not candidate.is_file():
            continue
        try:
            font = ImageFont.truetype(str(candidate), 24)
            first, second = font.getmask("中"), font.getmask("余")
            if bytes(first) and (first.size, bytes(first)) != (
                second.size,
                bytes(second),
            ):
                return candidate
        except (OSError, ValueError):
            continue
    raise ChineseFontUnavailable(
        "未找到中文字体，请配置 font_path 或安装 Noto Sans CJK；本次使用文字余额。"
    )


def _clean_label(value: str, *, fallback: str = "") -> str:
    text = "".join(
        char
        for char in str(value)[:512]
        if not unicodedata.category(char).startswith("C")
    )
    return " ".join(text.split()) or fallback


def _amount_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BalanceRenderError("余额必须是有限 Decimal 数值")
    if len(value.as_tuple().digits) > 128 or value.adjusted() > 60:
        raise BalanceRenderError("余额超出图片可显示范围，请使用文字余额")
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + 4, value.adjusted() + 4)
        # Match the existing Decimal text adapter, including half-cent ties.
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"{rounded:,.2f}"


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    *,
    start_size: int,
    min_size: int,
    max_width: int,
) -> ImageFont.FreeTypeFont:
    for size in range(start_size, min_size - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
    raise BalanceRenderError("文字超出气泡可显示范围，请使用文字余额")


def _line_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int, int, int]:
    box = draw.textbbox((CENTER_X, y), text, font=font, anchor="mm")
    left, top, right, bottom = TEXT_BOUNDS
    if not (left <= box[0] <= box[2] <= right and top <= box[1] <= box[3] <= bottom):
        raise BalanceRenderError("文字超出气泡安全区，请使用文字余额")
    return box


def _draw_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    font: ImageFont.FreeTypeFont,
    *,
    fill: str = INK,
) -> None:
    _line_box(draw, text, y, font)
    draw.text((CENTER_X, y), text, font=font, anchor="mm", fill=fill)


def _draw_balance_gradient(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    font: ImageFont.FreeTypeFont,
) -> None:
    """Apply a static left-to-right gradient only through the amount glyphs."""
    left, top, right, bottom = _line_box(draw, text, y, font)
    width, height = right - left, bottom - top
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text(
        (CENTER_X - left, y - top), text, font=font, anchor="mm", fill=255
    )
    colors = [
        tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
        for color in BALANCE_GRADIENT
    ]
    gradient = Image.new("RGB", (width, height))
    gradient_draw = ImageDraw.Draw(gradient)
    for x in range(width):
        progress = x * (len(colors) - 1) / max(1, width - 1)
        segment = min(int(progress), len(colors) - 2)
        fraction = progress - segment
        color = tuple(
            round(start + (end - start) * fraction)
            for start, end in zip(colors[segment], colors[segment + 1])
        )
        gradient_draw.line((x, 0, x, height - 1), fill=color)
    canvas.paste(gradient, (left, top), mask)


def render_balance_bubble(
    *,
    currency: str,
    total: Decimal,
    is_available: bool,
    query_stamp: str,
    cached: bool = False,
    font_path: str | Path | None = None,
    background_path: str | Path = BACKGROUND_PATH,
    preview_label: str | None = None,
    group_usage_text: str | None = None,
) -> bytes:
    """Return one currency's balance as PNG bytes without writing any files.

    ``query_stamp`` is the *original* query time formatted as Beijing local time
    ``YYYY-MM-DD HH:MM:SS`` (optionally followed by `` 北京时间``). A cache hit
    must pass the cached result's timestamp.
    ``group_usage_text`` is an optional, preformatted line supplied by the caller.
    This renderer does not measure group usage or estimate monetary spending.
    ``preview_label`` is exclusively for clearly labelled simulated previews;
    leave it unset for real responses. Values are rounded to two decimal places,
    never abbreviated. Oversized text and unavailable assets raise an error so
    the caller can send its original full text response instead.
    """
    if not isinstance(is_available, bool) or not isinstance(cached, bool):
        raise BalanceRenderError("API 可用和缓存状态必须是布尔值")
    if not isinstance(query_stamp, str):
        raise BalanceRenderError("查询时间必须为 YYYY-MM-DD HH:MM:SS 北京时间")
    date_text = query_stamp.removesuffix(" 北京时间")
    try:
        parsed_stamp = datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise BalanceRenderError("查询时间必须为 YYYY-MM-DD HH:MM:SS 北京时间") from exc
    if parsed_stamp.strftime("%Y-%m-%d %H:%M:%S") != date_text:
        raise BalanceRenderError("查询时间必须保留完整年月日和时分秒")

    if currency not in ("CNY", "USD"):
        raise BalanceRenderError("币种必须是 CNY 或 USD")
    amount = _amount_text(total)
    amount = f"¥ {amount}" if currency == "CNY" else f"{amount} USD"
    if group_usage_text is not None and (
        not isinstance(group_usage_text, str) or len(group_usage_text) > 512
    ):
        raise BalanceRenderError("群统计行必须是可完整显示的文字")
    group_usage = _clean_label(group_usage_text) if group_usage_text else ""
    font_file = resolve_font_path(font_path)
    with Image.open(background_path) as background:
        if background.size != CANVAS_SIZE:
            raise BalanceRenderError("背景必须是原始 1254 × 1254 气泡图")
        canvas = background.convert("RGB")
    draw = ImageDraw.Draw(canvas)

    title = "钱包余额"
    title_font = _fit_font(
        draw, title, font_file, start_size=28, min_size=20, max_width=450
    )
    _draw_line(draw, title, 172 if group_usage else 184, title_font, fill=TITLE_INK)

    amount_font = _fit_font(
        draw, amount, font_file, start_size=76, min_size=28, max_width=490
    )
    _draw_balance_gradient(
        canvas, draw, amount, 249 if group_usage else 269, amount_font
    )

    if group_usage:
        group_font = _fit_font(
            draw, group_usage, font_file, start_size=23, min_size=17, max_width=490
        )
        _draw_line(draw, group_usage, 315, group_font, fill=GROUP_USAGE_INK)

    status = []
    if not is_available:
        status.append("API 不可用")
    if cached:
        status.append("缓存结果")
    if status:
        _draw_line(
            draw,
            " · ".join(status),
            352 if group_usage else 330,
            ImageFont.truetype(str(font_file), 20),
            fill=UNAVAILABLE_INK if not is_available else SECONDARY_INK,
        )
    stamp = f"{date_text}（北京时间）"
    stamp_font = _fit_font(
        draw, stamp, font_file, start_size=17, min_size=15, max_width=490
    )
    _draw_line(draw, stamp, 389 if group_usage else 375, stamp_font, fill=SECONDARY_INK)

    if preview_label:
        label = _clean_label(preview_label)
        label_font = _fit_font(
            draw, label, font_file, start_size=19, min_size=15, max_width=450
        )
        _draw_line(draw, label, 434, label_font)

    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()
