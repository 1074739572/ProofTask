# -*- coding: utf-8 -*-
"""Render terminal char frames (with ANSI colors) to terminal-style PNG images."""
import re
import sys
from PIL import Image, ImageDraw, ImageFont

FONT_MONO = r"C:\Windows\Fonts\consola.ttf"
FONT_CJK  = r"C:\Windows\Fonts\msyh.ttc"
FONT_MONO_B = r"C:\Windows\Fonts\consolab.ttf"

CELL_W, CELL_H = 12, 22
PAD_X, PAD_Y = 16, 16
BG = (18, 20, 26, 255)          # terminal dark background
FG = (216, 222, 233, 255)       # default light text

PALETTE = {
    30: (60, 60, 60), 31: (255, 85, 85), 32: (80, 220, 130),
    33: (240, 200, 80), 34: (90, 160, 255), 35: (220, 120, 255),
    36: (80, 210, 230), 37: (216, 222, 233),
    90: (130, 130, 130), 91: (255, 140, 140), 92: (150, 255, 180),
    93: (255, 230, 130), 94: (140, 190, 255), 95: (240, 160, 255),
    96: (140, 230, 250), 97: (255, 255, 255),
}

def load_font(size):
    mono = ImageFont.truetype(FONT_MONO, size)
    cjk  = ImageFont.truetype(FONT_CJK, size)
    mono_b = ImageFont.truetype(FONT_MONO_B, size)
    return mono, cjk, mono_b

def char_width(ch, mono, cjk):
    return cjk.getlength(ch) if ord(ch) > 0x2E7F else mono.getlength(ch)

def render_frame(text_lines, out_path, title):
    mono, cjk, mono_b = load_font(16)
    # strip the ===== FRAME ... ===== and ===== END ===== markers
    lines = [l for l in text_lines if not l.startswith('=====')]
    # ANSI-aware cell rendering: each visible char painted onto a cell grid
    # First pass: compute per-line visual columns to keep CJK alignment.
    cell_rows = []  # list of (segments) where segment = (col, fg, char)
    for raw in lines:
        # parse ANSI
        segments = []
        x = 0
        fg = None
        i = 0
        # split by escape codes
        parts = re.split(r'(\x1b\[[0-9;]*m)', raw)
        for part in parts:
            if part.startswith('\x1b'):
                codes = part[2:-1] or '0'
                for c in codes.split(';'):
                    if c == '0':
                        fg = None
                    elif c in PALETTE:
                        fg = PALETTE[c]
                continue
            for ch in part:
                if ch == '\n':
                    continue
                w = 2 if ord(ch) > 0x2E7F else 1
                segments.append((x, fg, ch, w))
                x += w
        cell_rows.append(segments)
    max_cols = max((sum(seg[3] for seg in row) for row in cell_rows), default=0)
    nrows = len(cell_rows)
    img_w = PAD_X * 2 + max_cols * CELL_W
    img_h = PAD_Y * 2 + nrows * CELL_H
    img = Image.new('RGB', (img_w, img_h), BG)
    draw = ImageDraw.Draw(img)
    # subtle window frame like a terminal window
    draw.rectangle([0, 0, img_w - 1, 26], fill=(30, 33, 42))
    draw.text((14, 4), "●  terminal preview", font=cjk, fill=(140, 150, 170))
    draw.text((img_w - 180, 4), "harness tui", font=cjk, fill=(140, 150, 170))
    draw.rectangle([0, 26, img_w - 1, 26], fill=(52, 56, 68))
    y = PAD_Y + 26
    for row in cell_rows:
        for col, fg, ch, w in row:
            font = cjk if w == 2 else mono
            color = fg if fg else FG
            draw.text((PAD_X + col * CELL_W, y), ch, font=font, fill=color)
        y += CELL_H
    img.save(out_path)
    print(f"saved {out_path} {img_w}x{img_h}")

if __name__ == '__main__':
    src, out = sys.argv[1], sys.argv[2]
    with open(src, encoding='utf-8', errors='replace') as f:
        data = f.read()
    # split into frames by ===== FRAME
    blocks = re.split(r'^===== .*? =====$', data, flags=re.M)
    frames = [b for b in blocks if b.strip()]
    if not frames:
        print("no frames found")
        sys.exit(1)
    for idx, block in enumerate(frames):
        lines = block.strip('\n').split('\n')
        if len(frames) == 1:
            render_frame(lines, out, 'frame')
        else:
            render_frame(lines, out.replace('.png', f'_{idx+1}.png'), f'frame_{idx+1}')