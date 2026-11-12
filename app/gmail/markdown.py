"""HTML into Markdown, for email bodies (app/gmail/importer.py). Standard
library only (html.parser). Moved here from the OneNote import (removed in
5.8), which it was first written for.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Callable

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
    """(title, markdown) for an HTML page (an email). `image(src, alt)` saves an embedded image and
    returns how to embed it (e.g. "![[Page - image 1.png]]"), or "" to leave it out."""
    parser = _ToMarkdown(image)
    parser.feed(html)
    parser.close()
    parser._flush()
    text = "\n".join(parser.lines)
    text = re.sub(r"\*\*\s*\*\*|\[\]\([^)]*\)", "", text)   # empty bold, empty links
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return unescape(parser.title).strip(), text
