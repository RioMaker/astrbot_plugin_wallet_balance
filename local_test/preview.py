"""Render labelled simulated wallet balances onto the supplied original artwork."""

from __future__ import annotations

import argparse
import io
import sys
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from balance_renderer import TEXT_BOUNDS, render_balance_bubble  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--background", type=Path, default=ROOT / "assets/balance-bubble.jpg"
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "local_test/output")
    parser.add_argument("--font", type=Path)
    parser.add_argument(
        "--group-usage-text",
        default="本群今日估算 ¥ 0.0864",
        help="仅用于模拟排版的群统计文字；本脚本不统计或估算消费，空字符串可隐藏此行",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    examples = [
        (
            "wallet-bubble-preview.png",
            "128.60",
            False,
            True,
        ),
        (
            "wallet-bubble-cached-preview.png",
            "128.60",
            True,
            True,
        ),
        (
            "wallet-bubble-negative-preview.png",
            "-12.34",
            False,
            False,
        ),
        (
            "wallet-bubble-large-preview.png",
            "9876543210.12",
            True,
            True,
        ),
    ]
    with Image.open(args.background) as source:
        original = source.convert("RGB")
    for name, total, cached, available in examples:
        payload = render_balance_bubble(
            currency="CNY",
            total=Decimal(total),
            is_available=available,
            query_stamp="2026-10-29 15:20:00",
            cached=cached,
            font_path=args.font,
            background_path=args.background,
            preview_label="模拟数据 · 非真实余额",
            group_usage_text=args.group_usage_text,
        )
        path = args.output_dir / name
        path.write_bytes(payload)
        with Image.open(io.BytesIO(payload)) as result:
            assert result.size == original.size
            changed = ImageChops.difference(original, result).getbbox()
            assert changed is not None
            x0, y0, x1, y1 = TEXT_BOUNDS
            assert x0 <= changed[0] <= changed[2] <= x1
            assert y0 <= changed[1] <= changed[3] <= y1
        print(f"{path.resolve()} | changed pixels: {changed} | {len(payload):,} bytes")


if __name__ == "__main__":
    main()
