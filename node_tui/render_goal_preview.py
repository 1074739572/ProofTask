"""Render exported terminal spans JSON into PNG previews.

Usage: python render_goal_preview.py <spans.json> <out.png>
"""
import json
import sys
import unicodedata

from PIL import Image, ImageDraw, ImageFont

COL_W = 10
ROW_H = 21
PAD = 14

FONTS = {
    "ascii": ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 15),
    "symbol": ImageFont.truetype("C:/Windows/Fonts/seguisym.ttf", 15),
    "cjk": ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 16),
}


def display_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1


def pick_font(ch: str) -> ImageFont.FreeTypeFont:
    code = ord(ch)
    if display_width(ch) == 2:
        return FONTS["cjk"]
    if 0x2190 <= code <= 0x2BFF or code in (0x2588, 0x2591, 0x2592, 0x2593):
        return FONTS["symbol"]
    return FONTS["ascii"]


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as fh:
        frame = json.loads(fh.read().splitlines()[0])

    cols, rows = frame["cols"], frame["rows"]
    img = Image.new("RGB", (cols * COL_W + PAD * 2, rows * ROW_H + PAD * 2), "#1a1b26")
    draw = ImageDraw.Draw(img)

    for y, spans in enumerate(frame["lines"]):
        row_top = PAD + y * ROW_H
        col = 0
        for span in spans:
            fg, bg = span["fg"], span["bg"]
            for ch in span["text"]:
                if ch == " ":
                    if bg != "#000000":
                        draw.rectangle(
                            [PAD + col * COL_W, row_top, PAD + (col + 1) * COL_W - 1, row_top + ROW_H - 1],
                            fill=bg,
                        )
                    col += 1
                    continue
                w = display_width(ch)
                if bg != "#000000":
                    draw.rectangle(
                        [PAD + col * COL_W, row_top, PAD + (col + w) * COL_W - 1, row_top + ROW_H - 1],
                        fill=bg,
                    )
                font = pick_font(ch)
                draw.text((PAD + col * COL_W + 1, row_top + 2), ch, font=font, fill=fg)
                col += w

    img.save(dst)
    print(f"saved {dst} ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
