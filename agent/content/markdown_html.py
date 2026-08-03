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

from agent.content.visual_accents import (
    accent_for,
    highlighter_style,
    strong_style,
    tip_bar_style,
)


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


_TASK_CHECKBOX_RE = re.compile(r"^\[[ xX]\]\s*")


def _strip_task_checkbox(text: str) -> str:
    return _TASK_CHECKBOX_RE.sub("", str(text or "")).strip()


def _inline_md(text: str, *, seed: str = "") -> str:
    escaped = escape_html(text)

    def _image(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        clean_src = (
            src.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .strip()
        )
        clean_src = clean_src.split()[0] if clean_src.split() else clean_src
        final_src = clean_src
        if not re.match(r"^https?:|^data:", clean_src, re.I) and Path(clean_src).is_file():
            mime = mimetypes.guess_type(clean_src)[0] or "application/octet-stream"
            b64 = base64.b64encode(Path(clean_src).read_bytes()).decode("ascii")
            final_src = f"data:{mime};base64,{b64}"
        return (
            f'<img src="{escape_html(final_src)}" alt="{alt}" '
            f'style="width:100%;max-width:100%;height:auto;border-radius:12px;" />'
        )

    def _link(match: re.Match) -> str:
        label, href = match.group(1), match.group(2)
        clean_href = href.replace("&amp;", "&").strip()
        return (
            f'<a href="{escape_html(clean_href)}" '
            f'style="color:#1565c0;font-weight:600;text-decoration:underline;'
            f'text-underline-offset:3px;" target="_blank" rel="noopener noreferrer">'
            f"{label}</a>"
        )

    escaped = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _image, escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, escaped)

    def _strong(match: re.Match) -> str:
        value = match.group(1)
        return f'<strong style="{strong_style(value, seed=seed)}">{value}</strong>'

    escaped = re.sub(r"\*\*(.+?)\*\*", _strong, escaped)
    escaped = re.sub(
        r"`(.+?)`",
        r"<code style='background:#f3f4f6;padding:1px 5px;border-radius:4px;'>\1</code>",
        escaped,
    )
    return escaped


_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def h2_display_text(heading: str) -> str:
    """Display form of an H2 as rendered by markdown_to_html (number stripped)."""
    return re.sub(r"^\d+\.\s*", "", str(heading or "")).strip()


def _circled_number(num: int) -> str:
    return chr(0x245F + num) if 1 <= num <= 20 else f"{num}."


def h2_inner_html(heading: str, *, seed: str = "") -> str:
    """▶ + situational highlighter (color follows heading meaning)."""
    text = escape_html(h2_display_text(heading))
    accent = accent_for(h2_display_text(heading), seed=seed)
    return (
        f'<span style="color:{accent["ink"]};margin-right:6px;">▶</span>'
        f'<span style="{highlighter_style(h2_display_text(heading), seed=seed, padding="0 4px 3px")}">{text}</span>'
    )


def _split_table_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_table(rows: List[str]) -> str:
    """Render consecutive markdown table lines as an HTML table."""
    header_cells = _split_table_row(rows[0])
    body_rows = rows[1:]
    if len(rows) >= 2 and _TABLE_SEPARATOR_RE.match(rows[1].strip()):
        body_rows = rows[2:]
    # Rounded card table: green headers, hairline row dividers, no cell grid —
    # matches the reference blog's clean pharmacy-magazine look.
    parts = [
        '<div style="overflow-x:auto;margin:1.6em 0 2.1em;border:1px solid #e5e7eb;'
        'border-radius:14px;padding:6px 18px;box-shadow:0 1px 4px rgba(0,0,0,0.04);">',
        '<table style="border-collapse:collapse;width:100%;line-height:1.7;">',
        "<thead>",
        "<tr>",
    ]
    parts.extend(
        '<th style="padding:14px 12px;text-align:left;color:#1b5e20;font-weight:700;'
        'background:#edf6ed;border-bottom:2px solid #dcebdc;white-space:nowrap;">'
        f"{_inline_md(cell)}</th>"
        for cell in header_cells
    )
    parts.extend(["</tr>", "</thead>", "<tbody>"])
    for row in body_rows:
        if _TABLE_SEPARATOR_RE.match(row.strip()):
            continue
        parts.append("<tr>")
        cells = _split_table_row(row)
        for i, cell in enumerate(cells):
            first_style = "font-weight:700;color:#37474f;" if i == 0 else ""
            parts.append(
                '<td style="padding:13px 10px;vertical-align:top;'
                f'border-bottom:1px solid #f1f3f2;{first_style}">{_inline_md(cell)}</td>'
            )
        parts.append("</tr>")
    parts.extend(["</tbody>", "</table>", "</div>"])
    return "\n".join(parts)


def markdown_to_html(markdown: str) -> str:
    lines = strip_frontmatter(markdown).split("\n")
    html: List[str] = []
    in_list = False
    list_accent: Optional[dict] = None
    table_rows: List[str] = []
    doc_seed = extract_title(markdown, "") or (lines[0] if lines else "post")

    def close_list() -> None:
        nonlocal in_list, list_accent
        if in_list:
            html.append("</ul>")
            in_list = False
        list_accent = None

    def close_table() -> None:
        nonlocal table_rows
        if table_rows:
            html.append(_render_table(table_rows))
            table_rows = []

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_list()
            close_table()
            continue
        if line.lstrip().startswith("|"):
            close_list()
            table_rows.append(line.strip())
            continue
        close_table()
        for level, tag in ((6, "h6"), (5, "h5"), (4, "h4"), (3, "h3"), (2, "h2"), (1, "h1")):
            if re.match(rf"^#{{{level}}}\s+", line):
                close_list()
                heading_text = re.sub(rf"^#{{{level}}}\s+", "", line)
                accent = accent_for(heading_text, seed=doc_seed)
                if level == 1:
                    html.append(
                        '<h1 style="margin:0 0 1.1em;line-height:1.35;font-weight:800;'
                        f'color:{accent["ink"]};">'
                        f'<span style="{highlighter_style(heading_text, seed=doc_seed, padding="0 4px 3px")}">'
                        f'{escape_html(heading_text)}</span></h1>'
                    )
                elif level == 2:
                    html.append(
                        '<h2 style="margin:2.2em 0 0.9em;line-height:1.4;'
                        'font-weight:800;letter-spacing:-0.01em;color:#111;">'
                        f"{h2_inner_html(heading_text, seed=doc_seed)}</h2>"
                    )
                elif level == 3:
                    html.append(
                        '<h3 style="margin:1.7em 0 0.65em;line-height:1.45;font-weight:800;'
                        f'color:{accent["ink"]};">'
                        f'<span style="{highlighter_style(heading_text, seed=doc_seed)}'
                        f'padding:0 5px 3px;">{escape_html(heading_text)}</span></h3>'
                    )
                else:
                    html.append(
                        f'<{tag} style="margin:1.4em 0 0.6em;line-height:1.45;font-weight:700;">'
                        f"{escape_html(heading_text)}</{tag}>"
                    )
                break
        else:
            if re.match(r"^[-*]\s+", line):
                item_text = _strip_task_checkbox(re.sub(r"^[-*]\s+", "", line))
                if not in_list:
                    html.append(
                        '<ul style="margin:0.8em 0 1.6em;padding-left:1.4em;line-height:1.85;'
                        'list-style:none;">'
                    )
                    in_list = True
                    list_accent = accent_for(item_text, seed=doc_seed)
                html.append(
                    f'<li style="margin:0 0 0.55em;">'
                    f'<span style="color:{list_accent["ink"]};margin-right:8px;">●</span>'
                    f"{_inline_md(item_text, seed=doc_seed)}</li>"
                )
                continue
            num_match = re.match(r"^(\d+)\.\s+(.+)$", line)
            if num_match:
                if in_list:
                    html.append("</ul>")
                    in_list = False
                badge = _circled_number(int(num_match.group(1)))
                tip_text = num_match.group(2)
                tip_body = _inline_md(tip_text, seed=doc_seed)
                tip_accent = accent_for(tip_text, seed=doc_seed)
                html.append(
                    f'<p style="{tip_bar_style(tip_text, seed=doc_seed)}">'
                    f'<span style="color:{tip_accent["ink"]};font-weight:800;margin-right:8px;'
                    f'font-size:1.05em;">{badge}</span>'
                    f'<span style="font-weight:800;color:#111;">{tip_body}</span></p>'
                )
                continue
            if re.match(r"^:\s+", line):
                close_list()
                html.append(
                    '<p style="margin:0 0 1.15em 0.2em;line-height:1.85;color:#37474f;">'
                    f"{_inline_md(re.sub(r'^:\s+', '', line), seed=doc_seed)}</p>"
                )
                continue
            close_list()
            if re.match(r"^>\s+", line):
                quote = re.sub(r"^>\s+", "", line)
                q_accent = accent_for(quote, seed=doc_seed)
                html.append(
                    f'<blockquote style="margin:1.4em 0;padding:0.8em 1.1em;'
                    f'border-left:4px solid {q_accent["ink"]};background:{q_accent["tip"]};">'
                    f"{_inline_md(quote, seed=doc_seed)}</blockquote>"
                )
            elif re.match(r"^---+$", line):
                html.append('<hr style="border:none;border-top:1px solid #e5e7eb;margin:2em 0;">')
            else:
                html.append(
                    '<p style="margin:0 0 1.25em;line-height:1.9;color:#212121;">'
                    f"{_inline_md(line, seed=doc_seed)}</p>"
                )
            continue
        continue

    close_list()
    close_table()
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
    tag_match = (
        re.search(r"^태그:\s*(.+)$", markdown or "", flags=re.M)
        or re.search(r"^Tags?:\s*(.+)$", markdown or "", flags=re.M | re.I)
        or re.search(r"^\**\s*태그\s*후보\s*:?\**\s*:?\s*(.+)$", markdown or "", flags=re.M)
    )
    labels: List[str] = []
    if category_match:
        labels.append(category_match.group(1).strip())
    if tag_match:
        labels.extend(
            t.strip().lstrip("#")
            for t in re.split(r"[,#|/]", tag_match.group(1))
            if t.strip()
        )
    seen = []
    for label in labels:
        if label and label not in seen:
            seen.append(label)
    return seen[:max_labels]
