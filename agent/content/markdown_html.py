"""Markdown → HTML conversion — ported from
multi-content-pipeline/scripts/blogspot-draft-test.js (markdownToHtml et al.).

Pure text transform, no network, no LLM.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import List, Optional


def strip_frontmatter(markdown: str) -> str:
    return re.sub(r"^---[\s\S]*?---\s*", "", markdown or "").strip()


def escape_html(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _inline_md(text: str) -> str:
    escaped = escape_html(text)

    def _image(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        safe_alt = escape_html(alt)
        raw_src = re.sub(r"^&lt;|&gt;$", "", str(src)).strip()
        clean_src = raw_src.split()[0] if raw_src.split() else raw_src
        final_src = clean_src
        if not re.match(r"^https?:|^data:", clean_src, re.I) and Path(clean_src).is_file():
            mime = mimetypes.guess_type(clean_src)[0] or "application/octet-stream"
            b64 = base64.b64encode(Path(clean_src).read_bytes()).decode("ascii")
            final_src = f"data:{mime};base64,{b64}"
        return f'<img src="{final_src}" alt="{safe_alt}" style="max-width:100%;height:auto;" />'

    escaped = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _image, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return escaped


def markdown_to_html(markdown: str) -> str:
    lines = strip_frontmatter(markdown).split("\n")
    html: List[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        for level, tag in ((6, "h6"), (5, "h5"), (4, "h4"), (3, "h3"), (2, "h2"), (1, "h1")):
            if re.match(rf"^#{{{level}}}\s+", line):
                close_list()
                html.append(f"<{tag}>{escape_html(re.sub(rf'^#{{{level}}}\s+', '', line))}</{tag}>")
                break
        else:
            if re.match(r"^[-*]\s+", line):
                if not in_list:
                    html.append("<ul>")
                    in_list = True
                html.append(f"<li>{_inline_md(re.sub(r'^[-*]\s+', '', line))}</li>")
                continue
            close_list()
            if re.match(r"^>\s+", line):
                html.append(f"<blockquote>{_inline_md(re.sub(r'^>\s+', '', line))}</blockquote>")
            elif re.match(r"^---+$", line):
                html.append("<hr>")
            else:
                html.append(f"<p>{_inline_md(line)}</p>")
            continue
        continue

    close_list()
    return "\n".join(html)


def extract_title(markdown: str, fallback: str = "제목 없음") -> str:
    match = re.search(r"^#\s+(.+)$", markdown or "", flags=re.M) or re.search(
        r"^제목(?: \(H1\))?:\s*(.+)$", markdown or "", flags=re.M
    )
    title = match.group(1) if match else fallback
    return re.sub(r"[*_`]", "", title).strip()


def extract_labels(markdown: str, max_labels: int = 20) -> List[str]:
    category_match = re.search(r"^category_name:\s*(.+)$", markdown or "", flags=re.M) or re.search(
        r"^category:\s*(.+)$", markdown or "", flags=re.M
    )
    tag_match = re.search(r"^태그:\s*(.+)$", markdown or "", flags=re.M) or re.search(
        r"^Tags?:\s*(.+)$", markdown or "", flags=re.M | re.I
    )
    labels: List[str] = []
    if category_match:
        labels.append(category_match.group(1).strip())
    if tag_match:
        labels.extend(t.strip() for t in re.split(r"[#,]", tag_match.group(1)) if t.strip())
    seen = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    return seen[:max_labels]
