"""Deterministic HTML to Markdown conversion."""
import html as html_module
import re


_BLOCK_DROP_RE = re.compile(
    r"<(script|style|noscript|svg|nav|footer|form|iframe)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")


def _entity(text: str) -> str:
    return html_module.unescape(text)


def _inline_markdown(fragment: str) -> str:
    """行内元素（链接/强调）转换，保持可读与可追溯。"""
    text = fragment

    def _link(match: re.Match) -> str:
        href = match.group(1).strip()
        label = _TAG_STRIP_RE.sub("", match.group(2)).strip() or href
        return f"[{_entity(_WS_RE.sub(' ', label))}]({href})"

    text = re.sub(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                  _link, text, flags=re.I | re.S)
    text = re.sub(r"<(?:strong|b)\b[^>]*>(.*?)</(?:strong|b)>",
                  lambda m: f"**{m.group(1)}**", text, flags=re.I | re.S)
    text = re.sub(r"<(?:em|i)\b[^>]*>(.*?)</(?:em|i)>",
                  lambda m: f"*{m.group(1)}*", text, flags=re.I | re.S)
    text = re.sub(r"<code\b[^>]*>(.*?)</code>",
                  lambda m: f"`{m.group(1)}`", text, flags=re.I | re.S)
    text = _TAG_STRIP_RE.sub("", text)
    return _WS_RE.sub(" ", _entity(text)).strip()


def html_to_markdown(source: str) -> str:
    """确定性的 HTML → Markdown：保留标题、段落、列表、表格、链接与 DOI 等文字。

    不生成原文没有的标题，不删链接文字；表格按单元格导出为管道表，
    无法识别的标签一律退化为文本，绝不静默丢内容。
    """
    text = str(source or "")
    text = _BLOCK_DROP_RE.sub(" ", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)

    # 表格：先整块转成管道表，避免被后续段落替换拆散
    def _table(match: re.Match) -> str:
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", match.group(1), flags=re.I | re.S)
        if not rows:
            return " "
        out: list[str] = []
        width = 0
        parsed: list[list[str]] = []
        for row in rows:
            cells = re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", row, flags=re.I | re.S)
            cells = [_inline_markdown(cell).replace("|", "\\|") for cell in cells]
            if not cells:
                continue
            parsed.append(cells)
            width = max(width, len(cells))
        if not parsed:
            return " "
        for index, cells in enumerate(parsed):
            padded = cells + [""] * (width - len(cells))
            out.append("| " + " | ".join(padded) + " |")
            if index == 0:
                out.append("| " + " | ".join(["---"] * width) + " |")
        return "\n\n" + "\n".join(out) + "\n\n"

    text = re.sub(r"<table\b[^>]*>(.*?)</table>", _table, text, flags=re.I | re.S)

    for level in range(1, 7):
        text = re.sub(rf"<h{level}\b[^>]*>(.*?)</h{level}>",
                      lambda m, lv=level: "\n\n" + "#" * lv + " " + _inline_markdown(m.group(1)) + "\n\n",
                      text, flags=re.I | re.S)
    text = re.sub(r"<blockquote\b[^>]*>(.*?)</blockquote>",
                  lambda m: "\n\n" + "\n".join("> " + line for line in
                                               _inline_markdown(m.group(1)).splitlines() or [""]) + "\n\n",
                  text, flags=re.I | re.S)
    text = re.sub(r"<li\b[^>]*>(.*?)</li>",
                  lambda m: "\n- " + _inline_markdown(m.group(1)), text, flags=re.I | re.S)
    text = re.sub(r"</?(?:ul|ol)\b[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<hr\s*/?>", "\n\n---\n\n", text, flags=re.I)
    text = re.sub(r"</(?:p|div|section|article|main|header|figure|figcaption|td|th|tr)>", "\n\n", text, flags=re.I)
    # 剩余的行内元素（段落里的链接、加粗、行内代码）统一走行内转换，
    # 保证 <a> 的 href 不会在纯文本化时被丢掉。
    text = _inline_markdown(text)
    lines = []
    for line in text.splitlines():
        stripped = _WS_RE.sub(" ", line).rstrip()
        if stripped or (lines and lines[-1]):
            lines.append(stripped)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
