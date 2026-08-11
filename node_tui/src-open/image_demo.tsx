// Image render demo: renders a PNG in the terminal via half-block chars.
// Run with: bun src-open/image_demo.tsx [path/to/image.png]
// Each terminal character shows 2 pixel rows: top half uses the foreground
// color, bottom half the background color of the "▀" char.
import './env.ts';
import {createSignal, For, Show, onCleanup, onMount} from 'solid-js';
import {render, useRenderer} from '@opentui/solid';
import {inflateSync} from 'node:zlib';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';

const MAX_WIDTH = 100;
const MAX_HEIGHT = 60;

type Png = {width: number; height: number; rgba: Uint8Array};

// Minimal PNG decoder: IHDR + IDAT(zlib inflate) + unfilter. RGB/RGBA/8-bit
// only — enough for screenshots and photos exported by common tools.
function decodePng(buf: Uint8Array): Png {
  if (buf[0] !== 0x89 || buf[1] !== 0x50 || buf[2] !== 0x4e || buf[3] !== 0x47) throw new Error('not a PNG');
  let pos = 8;
  let width = 0, height = 0, bitDepth = 0, colorType = 0;
  const idat: Uint8Array[] = [];
  while (pos + 8 <= buf.length) {
    const len = (buf[pos] << 24) | (buf[pos + 1] << 16) | (buf[pos + 2] << 8) | buf[pos + 3];
    const type = String.fromCharCode(buf[pos + 4], buf[pos + 5], buf[pos + 6], buf[pos + 7]);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') {
      width = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3];
      height = (data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7];
      bitDepth = data[8]; colorType = data[9];
    } else if (type === 'IDAT') idat.push(data);
    pos += 12 + len;
  }
  if (!width || !height) throw new Error('no IHDR');
  if (bitDepth !== 8 || (colorType !== 6 && colorType !== 2)) throw new Error(`unsupported png ${bitDepth}bit colorType ${colorType}`);
  const bpp = colorType === 6 ? 4 : 3;
  const stride = width * bpp;
  const total = inflateSync(Buffer.concat(idat.map(x => Buffer.from(x))));
  const raw = new Uint8Array(total);
  const out = new Uint8Array(width * height * 4);
  let src = 0;
  let prev = new Uint8Array(stride);
  for (let y = 0; y < height; y++) {
    const filter = raw[src++];
    const line = raw.subarray(src, src + stride);
    src += stride;
    const recon = new Uint8Array(stride);
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? recon[x - bpp] : 0;
      const b = prev[x];
      const c = x >= bpp ? prev[x - bpp] : 0;
      let v = line[x];
      if (filter === 1) v = (v + a) & 255;
      else if (filter === 2) v = (v + b) & 255;
      else if (filter === 3) v = (v + ((a + b) >> 1)) & 255;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        v = (v + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c)) & 255;
      }
      recon[x] = v;
    }
    for (let x = 0; x < width; x++) {
      out[(y * width + x) * 4] = recon[x * bpp];
      out[(y * width + x) * 4 + 1] = recon[x * bpp + 1];
      out[(y * width + x) * 4 + 2] = recon[x * bpp + 2];
      out[(y * width + x) * 4 + 3] = colorType === 6 ? recon[x * bpp + 3] : 255;
    }
    prev = recon;
  }
  return {width, height, rgba: out};
}

// Nearest-neighbor downscale to fit the terminal while keeping aspect ratio.
function fit(img: Png, maxW: number, maxH: number): Png {
  let w = img.width, h = img.height;
  const scale = Math.min(1, maxW / w, maxH / h);
  if (scale >= 1) return img;
  w = Math.max(1, Math.round(w * scale));
  h = Math.max(1, Math.round(h * scale));
  const out = new Uint8Array(w * h * 4);
  for (let y = 0; y < h; y++) {
    const sy = Math.floor((y * img.height) / h);
    for (let x = 0; x < w; x++) {
      const sx = Math.floor((x * img.width) / w);
      const si = (sy * img.width + sx) * 4;
      const di = (y * w + x) * 4;
      out[di] = img.rgba[si]; out[di + 1] = img.rgba[si + 1]; out[di + 2] = img.rgba[si + 2]; out[di + 3] = img.rgba[si + 3];
    }
  }
  return {width: w, height: h, rgba: out};
}

const hex = (v: number) => v.toString(16).padStart(2, '0');
const toHex = (rgba: Uint8Array, i: number) => `#${hex(rgba[i])}${hex(rgba[i + 1])}${hex(rgba[i + 2])}`;

function ImageDemo(props: {path: string}) {
  const renderer = useRenderer();
  const [img, setImg] = createSignal<Png | null>(null);
  const [error, setError] = createSignal<string | null>(null);
  onMount(() => {
    try {
      const file = resolve(process.cwd(), props.path);
      const raw = new Uint8Array(readFileSync(file));
      const decoded = decodePng(raw);
      setImg(fit(decoded, MAX_WIDTH, MAX_HEIGHT));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  });
  const timer = setTimeout(() => { renderer.destroy(); process.exit(0); }, 8000);
  onCleanup(() => clearTimeout(timer));
  const rgba = () => img()?.rgba;
  const width = () => img()?.width ?? 0;
  const height = () => img()?.height ?? 0;
  return <box flexDirection="column" paddingX={1}>
    <text fg="#888">image_demo · {props.path}{img() ? ` · ${width()}x${height()}px → ${width()}x${Math.ceil(height() / 2)} chars` : ''} · 8s 后自动退出</text>
    <Show when={error()}><text fg="#f44">解码失败: {error()}</text></Show>
    <Show when={img()}>
      <For each={Array.from({length: Math.ceil(height() / 2)}, (_, y) => y)}>{y =>
        <box flexDirection="row" minWidth={0}>
          <For each={Array.from({length: width()}, (_, x) => x)}>{x =>
            <text fg={toHex(rgba()!, (y * 2 * width() + x) * 4)} bg={y * 2 + 1 < height() ? toHex(rgba()!, ((y * 2 + 1) * width() + x) * 4) : undefined}>▀</text>
          }</For>
        </box>
      }</For>
    </Show>
  </box>;
}

await render(() => <ImageDemo path={process.argv[2] || '_ref/opencode/screenshot-uk.png'} />, {exitOnCtrlC: true, targetFps: 30});
