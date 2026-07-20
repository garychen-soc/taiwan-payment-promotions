from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin


BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}


@dataclass(slots=True)
class ParsedHTML:
    title: str
    text: str
    heading: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)


class VisibleHTMLParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self._meta_title = ""
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._first_heading = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "title":
            self._title_depth += 1
        if tag == "meta" and attr_map.get("property", "").lower() == "og:title":
            self._meta_title = attr_map.get("content", "")
        if tag in {"h1", "h2"} and not self._first_heading and self._heading_tag is None:
            self._heading_tag = tag
            self._heading_parts = []
        if tag == "a" and attr_map.get("href"):
            self._anchor_href = urljoin(self.base_url, attr_map["href"])
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag == "a" and self._anchor_href:
            anchor_text = _clean_inline(" ".join(self._anchor_parts))
            self.links.append((self._anchor_href, anchor_text))
            self._anchor_href = None
            self._anchor_parts = []
        if tag == self._heading_tag:
            self._first_heading = _clean_inline(" ".join(self._heading_parts))
            self._heading_tag = None
            self._heading_parts = []
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = html.unescape(data)
        self.parts.append(value)
        if self._title_depth:
            self._title_parts.append(value)
        if self._anchor_href:
            self._anchor_parts.append(value)
        if self._heading_tag:
            self._heading_parts.append(value)

    def parsed(self) -> ParsedHTML:
        raw_text = "".join(self.parts).replace("\xa0", " ")
        lines = [_clean_inline(line) for line in raw_text.splitlines()]
        text = "\n".join(line for line in lines if line)
        title = _clean_inline(self._meta_title or " ".join(self._title_parts))
        return ParsedHTML(title=title, text=text, heading=self._first_heading, links=self.links)


def _clean_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_html(source: str, base_url: str) -> ParsedHTML:
    parser = VisibleHTMLParser(base_url)
    parser.feed(source)
    parser.close()
    return parser.parsed()


def best_title(parsed: ParsedHTML) -> str:
    lines = parsed.text.splitlines()
    noisy_suffixes = [" | ", "｜", " - ", "_"]
    title = parsed.title
    title_is_generic = (
        not title
        or len(title) < 4
        or title.startswith("活動訊息")
        or title.strip().lower() in {"最新消息", "優惠活動", "活動內容"}
    )
    if title_is_generic and 4 <= len(parsed.heading) <= 180:
        return parsed.heading[:240]
    for separator in noisy_suffixes:
        if separator in title:
            first = title.split(separator, 1)[0].strip()
            if len(first) >= 4:
                title = first
                break
    if not title or len(title) < 4:
        title = next((line for line in lines if 4 <= len(line) <= 120), "未辨識活動名稱")
    return title[:240]
