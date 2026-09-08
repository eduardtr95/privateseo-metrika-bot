from __future__ import annotations

import html
from html.parser import HTMLParser


def fit_html(value: str, limit: int = 4096) -> str:
    """Limit Telegram's UTF-16 visible text, preserving complete tags and entities."""

    class Fit(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self.stack = []
            self.remaining = limit - 1
            self.truncated = False

        def handle_starttag(self, tag, attrs):
            if self.truncated:
                return
            if tag not in {"b", "i", "a", "strong", "em", "code", "pre", "u", "s", "blockquote"}:
                return
            attributes = "".join(
                f' {k}="{html.escape(v or "", quote=True)}"'
                for k, v in attrs
                if k in {"href", "class"}
            )
            self.parts.append(f"<{tag}{attributes}>")
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if self.truncated or tag not in self.stack:
                return
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f"</{current}>")
                if current == tag:
                    break

        def handle_data(self, data):
            if self.truncated:
                return
            chars = []
            for char in data:
                size = len(char.encode("utf-16-le")) // 2
                if size > self.remaining:
                    self.truncated = True
                    break
                chars.append(char)
                self.remaining -= size
            self.parts.append(html.escape("".join(chars)))

    parser = Fit()
    parser.feed(value)
    parser.close()
    if parser.truncated:
        parser.parts.append("…")
    parser.parts.extend(f"</{tag}>" for tag in reversed(parser.stack))
    return "".join(parser.parts)
