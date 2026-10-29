import io
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import balance_renderer as renderer  # noqa: E402


@pytest.fixture
def font():
    try:
        return renderer.resolve_font_path()
    except renderer.ChineseFontUnavailable:
        pytest.skip("A CJK font is needed for real rendering checks")


def values(**overrides):
    return (
        dict(
            currency="CNY",
            total=Decimal("128.60"),
            is_available=True,
            query_stamp="2026-10-29 15:20:00 北京时间",
        )
        | overrides
    )


@pytest.mark.parametrize("amount", ["128.60", "-0.01", "1000000000000000"])
def test_changes_only_bubble_pixels_and_never_creates_files(
    font, tmp_path, monkeypatch, amount
):
    monkeypatch.chdir(tmp_path)
    payload = renderer.render_balance_bubble(
        **values(total=Decimal(amount)),
        font_path=font,
    )
    with (
        Image.open(io.BytesIO(payload)) as rendered,
        Image.open(renderer.BACKGROUND_PATH) as original,
    ):
        assert rendered.format == "PNG" and rendered.size == original.size == (
            1254,
            1254,
        )
        box = ImageChops.difference(rendered, original.convert("RGB")).getbbox()
        assert box is not None
        left, top, right, bottom = renderer.TEXT_BOUNDS
        assert left <= box[0] <= box[2] <= right
        assert top <= box[1] <= box[3] <= bottom
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("currency", "amount", "expected"),
    [("CNY", "1.005", "¥ 1.00"), ("USD", "1.015", "1.02 USD")],
)
def test_actual_text_has_only_title_half_even_amount_and_full_query_time(
    font, monkeypatch, currency, amount, expected
):
    texts = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, text, *args, **kwargs):
        texts.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    renderer.render_balance_bubble(
        **values(
            currency=currency,
            total=Decimal(amount),
        ),
        font_path=font,
    )
    assert texts == ["钱包余额", expected, "2026-10-29 15:20:00（北京时间）"]


@pytest.mark.parametrize(
    ("available", "cached", "status"),
    [
        (True, False, None),
        (True, True, "缓存结果"),
        (False, False, "API 不可用"),
        (False, True, "API 不可用 · 缓存结果"),
    ],
)
def test_extra_status_is_rendered_only_for_cache_or_unavailable(
    font, monkeypatch, available, cached, status
):
    texts = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, text, *args, **kwargs):
        texts.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    renderer.render_balance_bubble(
        **values(is_available=available, cached=cached), font_path=font
    )
    expected = ["钱包余额", "¥ 128.60"]
    if status:
        expected.append(status)
    expected.append("2026-10-29 15:20:00（北京时间）")
    assert texts == expected


def test_missing_font_and_background_are_explicit_errors(tmp_path, font):
    with pytest.raises(renderer.ChineseFontUnavailable):
        renderer.render_balance_bubble(**values(), font_path=tmp_path / "missing.ttf")
    with pytest.raises(FileNotFoundError):
        renderer.render_balance_bubble(
            **values(), font_path=font, background_path=tmp_path / "missing.jpg"
        )


@pytest.mark.parametrize(
    "amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("1e61")]
)
def test_invalid_amounts_fail_before_render(amount):
    with pytest.raises(renderer.BalanceRenderError):
        renderer.render_balance_bubble(**values(total=amount))


@pytest.mark.parametrize(
    "group_line",
    [
        "本群今日估算 ¥ 0.0864",
        "本群今日估算 0.0864 USD · 部分未计价",
        "群消耗统计未就绪 · 记录不完整",
    ],
)
def test_group_line_and_status_fit_inside_original_bubble(font, group_line):
    payload = renderer.render_balance_bubble(
        **values(is_available=False, cached=True),
        font_path=font,
        group_usage_text=group_line,
        preview_label="模拟数据 · 非真实余额",
    )
    with (
        Image.open(io.BytesIO(payload)) as rendered,
        Image.open(renderer.BACKGROUND_PATH) as original,
    ):
        box = ImageChops.difference(rendered, original.convert("RGB")).getbbox()
        left, top, right, bottom = renderer.TEXT_BOUNDS
        assert left <= box[0] <= box[2] <= right
        assert top <= box[1] <= box[3] <= bottom
