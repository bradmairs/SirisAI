"""OneNote page content into Siris notes: the page HTML into Markdown, and
handwriting (InkML strokes) into a PNG the vision model can read.

No third-party libraries: html.parser for the HTML, and a small stroke
rasterizer with a hand-written PNG encoder for the ink, so the import needs
nothing SirisAI doesn't already have.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
import zlib
from html import unescape
from html.parser import HTMLParser
from typing import Callable

# ---------- HTML to Markdown ----------

_BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "ul", "ol", "br", "body"}


class _ToMarkdown(HTMLParser):
    def __init__(self, image: Callable[[str, str], str]) -> None:
        super().__init__(convert_charrefs=True)
        self.image = image
        self.title = ""
        self.lines: list[str] = []
        self.buf: list[str] = []
        self.prefix = ""          # what the current block starts with ("## ", "- [ ] ", "  - ")
        self.lists: list[list] = []   # [kind, counter]
        self.in_title = self.skip = False
        self.table: list[list[str]] | None = None
        self.cell: list[str] | None = None
        self.link: str | None = None

    def _flush(self) -> None:
        text = re.sub(r"[ \t]+", " ", "".join(self.buf)).strip()
        if text:
            self.lines.append(self.prefix + text)
        self.buf, self.prefix = [], ""

    def _block_prefix(self, tag: str, attrs: dict) -> str:
        level = tag[1] if re.fullmatch(r"h[1-6]", tag) else ""
        tags = attrs.get("data-tag") or ""
        todo = "- [x] " if "to-do:completed" in tags else "- [ ] " if "to-do" in tags else ""
        if level:
            return "#" * int(level) + " " + todo.replace("- ", "")
        if tag == "li" and self.lists:
            kind = self.lists[-1]
            kind[1] += 1
            bullet = f"{kind[1]}. " if kind[0] == "ol" else "- "
            return "  " * (len(self.lists) - 1) + (todo or bullet)
        return todo

    def handle_starttag(self, tag: str, attrs_list: list) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if tag in ("style", "script"):
            self.skip = True
        if tag == "title":
            self.in_title = True
            return
        if self.cell is not None and tag not in ("td", "th", "tr", "table"):
            if tag == "br":
                self.cell.append(" ")
            return
        if tag == "table":
            self._flush()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.table.append([])
        elif tag in ("td", "th") and self.table is not None:
            self.cell = []
        elif tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag, 0])
        elif tag in _BLOCKS:
            self._flush()
            if tag != "br":
                self.prefix = self._block_prefix(tag, attrs)
        elif tag in ("b", "strong"):
            self.buf.append("**")
        elif tag in ("i", "em"):
            self.buf.append("*")
        elif tag == "a":
            self.link = attrs.get("href")
            self.buf.append("[")
        elif tag == "img":
            src = attrs.get("data-fullres-src") or attrs.get("src") or ""
            embed = self.image(src, attrs.get("alt", "")) if src else ""
            if embed:
                self._flush()
                self.lines.append(embed)
        elif tag == "object":
            name = attrs.get("data-attachment") or "file"
            self.buf.append(f"(attachment: {name})")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script"):
            self.skip = False
        if tag == "title":
            self.in_title = False
            return
        if tag in ("td", "th") and self.cell is not None and self.table is not None:
            self.table[-1].append(re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "\\|"))
            self.cell = None
        elif tag == "table" and self.table is not None:
            rows = [r for r in self.table if r]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                self.lines.append("| " + " | ".join(rows[0]) + " |")
                self.lines.append("|" + " --- |" * width)
                self.lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            self.table = None
        elif self.cell is not None:
            return
        elif tag in ("ul", "ol") and self.lists:
            self._flush()
            self.lists.pop()
        elif tag in _BLOCKS:
            self._flush()
        elif tag in ("b", "strong"):
            self.buf.append("**")
        elif tag in ("i", "em"):
            self.buf.append("*")
        elif tag == "a":
            self.buf.append(f"]({self.link})" if self.link else "]")
            self.link = None

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif self.skip:
            return
        elif self.cell is not None:
            self.cell.append(data)
        else:
            self.buf.append(data)


def html_to_markdown(html: str, image: Callable[[str, str], str] = lambda src, alt: "") -> tuple[str, str]:
    """(title, markdown) for a OneNote page. `image(src, alt)` saves an embedded image and
    returns how to embed it (e.g. "![[Page - image 1.png]]"), or "" to leave it out."""
    parser = _ToMarkdown(image)
    parser.feed(html)
    parser.close()
    parser._flush()
    text = "\n".join(parser.lines)
    text = re.sub(r"\*\*\s*\*\*|\[\]\([^)]*\)", "", text)   # empty bold, empty links
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return unescape(parser.title).strip(), text


# ---------- handwriting: InkML strokes to PNG ----------

_VALUE = re.compile(r"[!'\"]?-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[?*]")


def parse_inkml(inkml: str) -> list[list[tuple[float, float]]]:
    """Each pen stroke as a list of (x, y) points. Decodes InkML's explicit (!), first-difference
    (') and second-difference (\") values, which persist per channel until changed."""
    try:
        root = ET.fromstring(inkml)
    except ET.ParseError:
        return []
    strokes = []
    for trace in root.iter():
        if not trace.tag.endswith("trace") or not (trace.text or "").strip():
            continue
        mode, prev, vel = ["!", "!"], [0.0, 0.0], [0.0, 0.0]
        points = []
        for raw in trace.text.split(","):
            tokens = _VALUE.findall(raw)[:2]
            if len(tokens) < 2:
                continue
            xy = []
            for ch, tok in enumerate(tokens):
                if tok in ("?", "*"):
                    xy.append(prev[ch] + (vel[ch] if tok == "*" else 0))
                    continue
                if tok[0] in "!'\"":
                    mode[ch], tok = tok[0], tok[1:]
                value = float(tok)
                if mode[ch] == "!":
                    vel[ch], prev[ch] = value - prev[ch], value
                else:
                    vel[ch] = value if mode[ch] == "'" else vel[ch] + value
                    prev[ch] += vel[ch]
                xy.append(prev[ch])
            points.append((xy[0], xy[1]))
        if points:
            strokes.append(points)
    return strokes


def _png(width: int, height: int, pixels: bytearray) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes(pixels[y * width:(y + 1) * width]) for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def render_strokes(strokes: list[list[tuple[float, float]]], width: int = 1400, tile_height: int = 1800,
                   max_tiles: int = 8, pen: int = 2) -> list[bytes]:
    """Dark strokes on white, scaled to `width` pixels across, as grayscale PNGs. A tall page is
    cut into tiles of about `tile_height`, at blank rows between lines of writing, because vision
    models shrink a tall image until handwriting is unreadable."""
    pts = [p for s in strokes for p in s]
    if not pts:
        return []
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, y0 = min(xs), min(ys)
    span_x, span_y = max(xs) - x0 or 1, max(ys) - y0 or 1
    margin = 30
    scale = (width - 2 * margin) / span_x
    scale = min(scale, (tile_height * max_tiles - 2 * margin) / span_y)
    w = int(span_x * scale) + 2 * margin
    h = int(span_y * scale) + 2 * margin
    px = bytearray(b"\xff" * (w * h))

    def dot(x: int, y: int) -> None:
        for dy in range(-pen, pen + 1):
            row = y + dy
            if 0 <= row < h:
                lo, hi = max(0, x - pen), min(w, x + pen + 1)
                px[row * w + lo:row * w + hi] = b"\x00" * (hi - lo)

    for stroke in strokes:
        line = [(int((x - x0) * scale) + margin, int((y - y0) * scale) + margin) for x, y in stroke]
        if len(line) == 1:
            dot(*line[0])
        for (ax, ay), (bx, by) in zip(line, line[1:]):
            steps = max(abs(bx - ax), abs(by - ay), 1)
            for i in range(steps + 1):
                dot(ax + (bx - ax) * i // steps, ay + (by - ay) * i // steps)

    blank = [b"\x00" not in px[y * w:(y + 1) * w] for y in range(h)]
    tiles, top = [], 0
    while top < h:
        cut = min(top + tile_height, h)
        if cut < h:   # back up to the nearest blank row, so a line of writing isn't split
            for y in range(cut, max(top + tile_height // 2, top + 1), -1):
                if blank[y]:
                    cut = y
                    break
        tiles.append(_png(w, cut - top, px[top * w:cut * w]))
        top = cut
    return tiles
