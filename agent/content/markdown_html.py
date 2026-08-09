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


def _inline_md(text: str, *, seed: str = "", plain: bool = False) -> str:
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
        if plain:
            return f'<a href="{escape_html(clean_href)}">{label}</a>'
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
        if plain:
            return f"<strong>{value}</strong>"
        return f'<strong style="{strong_style(value, seed=seed)}">{value}</strong>'

    escaped = re.sub(r"\*\*(.+?)\*\*", _strong, escaped)
    def _highlight(match: re.Match) -> str:
        style = (
            "background-color:#f1f2ca;font-weight:bold;"
            if plain
            else "background-color:#f1f2ca;"
        )
        return f'<span style="{style}">{match.group(1)}</span>'

    escaped = re.sub(r"==([^=\n]+)==", _highlight, escaped)
    if plain:
        escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
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
    """Render H2 text without a decorative inner highlight span."""
    return _inline_md(h2_display_text(heading), seed=seed)


def _split_table_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_table(rows: List[str], *, plain: bool = False) -> str:
    """Render consecutive markdown table lines as an HTML table."""
    header_cells = _split_table_row(rows[0])
    body_rows = rows[1:]
    if len(rows) >= 2 and _TABLE_SEPARATOR_RE.match(rows[1].strip()):
        body_rows = rows[2:]
    # Rounded card table: green headers, hairline row dividers, no cell grid —
    # matches the reference blog's clean pharmacy-magazine look.
    parts = []
    if not plain:
        parts.append(
            '<div style="overflow-x:auto;margin:1.6em 0 2.1em;border:1px solid #e5e7eb;'
            'border-radius:14px;padding:6px 18px;box-shadow:0 1px 4px rgba(0,0,0,0.04);">'
        )
    parts.extend([
        '<table style="border-collapse:collapse;width:100%;line-height:1.7;">',
        "<thead>",
        "<tr>",
    ])
    parts.extend(
        '<th style="padding:14px 12px;text-align:left;color:#1b5e20;font-weight:700;'
        'background:#edf6ed;border-bottom:2px solid #dcebdc;white-space:nowrap;">'
        f"{_inline_md(cell, plain=plain)}</th>"
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
                f'border-bottom:1px solid #f1f3f2;{first_style}">{_inline_md(cell, plain=plain)}</td>'
            )
        parts.append("</tr>")
    parts.extend(["</tbody>", "</table>"])
    if not plain:
        parts.append("</div>")
    return "\n".join(parts)


def markdown_to_html(markdown: str, *, plain: bool = False) -> str:
    lines = strip_frontmatter(markdown).split("\n")
    html: List[str] = []
    in_list = False
    plain_ordered_list = False
    list_accent: Optional[dict] = None
    table_rows: List[str] = []
    doc_seed = extract_title(markdown, "") or (lines[0] if lines else "post")

    def close_list() -> None:
        nonlocal in_list, list_accent
        if in_list:
            html.append("</ul>")
            in_list = False
        list_accent = None

    def close_plain_ordered_list() -> None:
        nonlocal plain_ordered_list
        if plain_ordered_list:
            html.append("</ol>")
            plain_ordered_list = False

    def close_table() -> None:
        nonlocal table_rows
        if table_rows:
            html.append(_render_table(table_rows, plain=plain))
            table_rows = []

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_list()
            if plain:
                close_plain_ordered_list()
            close_table()
            continue
        if line.lstrip().startswith("|"):
            close_list()
            if plain:
                close_plain_ordered_list()
            table_rows.append(line.strip())
            continue
        close_table()
        for level, tag in ((6, "h6"), (5, "h5"), (4, "h4"), (3, "h3"), (2, "h2"), (1, "h1")):
            if re.match(rf"^#{{{level}}}\s+", line):
                close_list()
                if plain:
                    close_plain_ordered_list()
                heading_text = re.sub(rf"^#{{{level}}}\s+", "", line)
                accent = accent_for(heading_text, seed=doc_seed)
                if level == 1:
                    heading_inner = (
                        _inline_md(heading_text, seed=doc_seed, plain=plain)
                        if plain
                        else (
                            f'<span style="{highlighter_style(heading_text, seed=doc_seed, padding="0 4px 3px")}">'
                            f'{escape_html(heading_text)}</span>'
                        )
                    )
                    html.append(
                        '<h1 style="margin:0 0 1.1em;line-height:1.35;font-weight:800;'
                        f'color:{accent["ink"]};">'
                        f"{heading_inner}</h1>"
                    )
                elif level == 2:
                    heading_inner = (
                        _inline_md(h2_display_text(heading_text), seed=doc_seed, plain=plain)
                        if plain
                        else h2_inner_html(heading_text, seed=doc_seed)
                    )
                    if plain:
                        html.append(f"<h2>{heading_inner}</h2>")
                    else:
                        html.append(f'<h2 style="{_TISTORY_H2_STYLE}">{heading_inner}</h2>')
                elif level == 3:
                    heading_inner = _inline_md(heading_text, seed=doc_seed, plain=plain)
                    if plain:
                        html.append(f"<h3>{heading_inner}</h3>")
                    else:
                        html.append(f'<h3 style="{_TISTORY_H3_STYLE}">{heading_inner}</h3>')
                else:
                    html.append(
                        f'<{tag} style="margin:1.4em 0 0.6em;line-height:1.45;font-weight:700;">'
                        f"{escape_html(heading_text)}</{tag}>"
                    )
                break
        else:
            if re.match(r"^[-*]\s+", line):
                item_text = _strip_task_checkbox(re.sub(r"^[-*]\s+", "", line))
                if plain:
                    close_plain_ordered_list()
                    if not in_list:
                        html.append('<ul style="list-style-type:disc;">')
                        in_list = True
                    html.append(f"<li>{_inline_md(item_text, seed=doc_seed, plain=plain)}</li>")
                    continue
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
                if plain:
                    close_list()
                    if not plain_ordered_list:
                        html.append('<ol style="list-style-type:decimal;">')
                        plain_ordered_list = True
                    tip_text = num_match.group(2)
                    html.append(
                        f"<li>{_inline_md(tip_text, seed=doc_seed, plain=plain)}</li>"
                    )
                    continue
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
                if plain:
                    close_plain_ordered_list()
                colon_text = re.sub(r"^:\s+", "", line)
                html.append(
                    '<p style="margin:0 0 1.15em 0.2em;line-height:1.85;color:#37474f;">'
                    f"{_inline_md(colon_text, seed=doc_seed, plain=plain)}</p>"
                )
                continue
            close_list()
            if plain:
                close_plain_ordered_list()
            if re.match(r"^>\s+", line):
                quote = re.sub(r"^>\s+", "", line)
                if plain:
                    html.append(
                        f"<blockquote>{_inline_md(quote, seed=doc_seed, plain=plain)}</blockquote>"
                    )
                else:
                    q_accent = accent_for(quote, seed=doc_seed)
                    html.append(
                        f'<blockquote style="margin:1.4em 0;padding:0.8em 1.1em;'
                        f'border-left:4px solid {q_accent["ink"]};background:{q_accent["tip"]};">'
                        f"{_inline_md(quote, seed=doc_seed)}</blockquote>"
                    )
            elif re.match(r"^---+$", line):
                html.append('<hr style="border:none;border-top:1px solid #e5e7eb;margin:2em 0;">')
            else:
                if plain:
                    html.append(f"<p>{_inline_md(line, seed=doc_seed, plain=plain)}</p>")
                else:
                    html.append(
                        '<p style="margin:0 0 1.25em;line-height:1.9;color:#212121;">'
                        f"{_inline_md(line, seed=doc_seed)}</p>"
                    )
            continue
        continue

    close_list()
    if plain:
        close_plain_ordered_list()
    close_table()
    return "\n".join(html)


_TISTORY_H2_STYLE = "border-left:6px solid #2f6fa8;padding-left:14px;margin-top:50px;color:#16324f;"
_TISTORY_H3_STYLE = "color:#1b4f80;margin-top:34px;"
_TISTORY_TABLE_STYLE = "width:100%;border-collapse:collapse;margin:22px 0;"
_TISTORY_TH_STYLE = (
    "border:1px solid #dde3ea;padding:10px 12px;text-align:left;"
    "background-color:#eef5fb;color:#1b4f80;"
)
_TISTORY_TD_STYLE = "border:1px solid #dde3ea;padding:10px 12px;"
_TISTORY_BLOCKQUOTE_STYLE = (
    "background-color:#f5f6f8;border:1px solid #e1e4e8;padding:16px 18px;"
    "margin:20px 0;border-radius:4px;text-align:center;font-weight:bold;color:#333333;"
)
_TISTORY_TOC_STYLE = (
    "background-color:#f7f9fb;border:1px solid #e3e8ee;padding:18px 24px;"
    "margin:28px 0;border-radius:6px;"
)
_TISTORY_CHECKLIST_STYLE = (
    "background-color:#f6faf6;border:1px solid #d7e8d7;padding:20px 24px;"
    "margin:26px 0;border-radius:6px;"
)
_TISTORY_CORE_STYLE = (
    "background-color:#eef5fb;border-left:5px solid #2f6fa8;padding:16px 20px;"
    "margin:26px 0;border-radius:4px;"
)
_TISTORY_CAUTION_STYLE = (
    "background-color:#fff6e5;border-left:5px solid #e09b2d;padding:16px 20px;"
    "margin:26px 0;border-radius:4px;"
)
_TISTORY_DISCLAIMER_STYLE = (
    "background-color:#f5f5f5;border:1px dashed #cccccc;padding:14px 18px;"
    "margin:24px 0;border-radius:4px;color:#666666;font-size:0.95em;"
)


def _wrap_heading_bold(inner: str) -> str:
    if "<b>" in inner or "<strong" in inner:
        return inner
    return f"<b>{inner}</b>"


def _replace_tistory_heading_styles(html: str) -> str:
    h2_number = 0

    def replace_h2(match: re.Match) -> str:
        nonlocal h2_number
        inner = match.group(1)
        plain_text = re.sub(r"<[^>]+>", "", inner).replace("▶", "").strip()
        number = ""
        if plain_text != "목차":
            h2_number += 1
            number = f"{h2_number}. "
        inner = re.sub(r"color\s*:[^;\"]+;?", "color:#16324f;", inner)
        return f'<h2 style="{_TISTORY_H2_STYLE}">{_wrap_heading_bold(f"{number}{inner}")}</h2>'

    def replace_h3(match: re.Match) -> str:
        inner = re.sub(r"color\s*:[^;\"]+;?", "color:#1b4f80;", match.group(1))
        return f'<h3 style="{_TISTORY_H3_STYLE}">{_wrap_heading_bold(inner)}</h3>'

    html = re.sub(r"<h2\b[^>]*>(.*?)</h2>", replace_h2, html, flags=re.S)
    return re.sub(r"<h3\b[^>]*>(.*?)</h3>", replace_h3, html, flags=re.S)


def _replace_tistory_table_styles(html: str) -> str:
    def style_table(match: re.Match) -> str:
        table = re.sub(r"<table\b[^>]*>", f'<table style="{_TISTORY_TABLE_STYLE}">', match.group(0), count=1)
        table = re.sub(r"<th\b[^>]*>", f'<th style="{_TISTORY_TH_STYLE}">', table)

        def style_row(row_match: re.Match) -> str:
            column = 0

            def style_cell(cell_match: re.Match) -> str:
                nonlocal column
                first_column = "font-weight:bold;" if column == 0 else ""
                column += 1
                return f'<td style="{_TISTORY_TD_STYLE}{first_column}">'

            return re.sub(r"<td\b[^>]*>", style_cell, row_match.group(0))

        return re.sub(r"<tr\b[^>]*>.*?</tr>", style_row, table, flags=re.S)

    return re.sub(r"<table\b[^>]*>.*?</table>", style_table, html, flags=re.S)


def _replace_tistory_callout(html: str, label: str, box_style: str, label_style: str) -> str:
    pattern = re.compile(
        rf"<p\b[^>]*><strong\b[^>]*>{re.escape(label)}(.*?)</strong></p>",
        flags=re.S,
    )
    return pattern.sub(
        lambda match: (
            f'<div style="{box_style}">{match.group(1).strip()}</div>'
            if label == "면책:"
            else (
                f'<div style="{box_style}">'
                f'<span style="{label_style}">{label[:-1]} &middot; '
                f'{match.group(1).strip()}</span></div>'
            )
        ),
        html,
    )


def _replace_tistory_image_placeholders(html: str) -> str:
    pattern = re.compile(
        r'<p\b[^>]*>::이미지::</p>\s*'
        r'<p\b[^>]*>설명:\s*(.*?)</p>\s*'
        r'<p\b[^>]*>프롬프트:\s*(.*?)</p>\s*'
        r'<p\b[^>]*>::이미지끝::</p>',
        flags=re.S,
    )
    image_number = 0

    def replace_image(match: re.Match) -> str:
        nonlocal image_number
        image_number += 1
        description = match.group(1).strip()
        prompt = match.group(2).strip()
        return (
            '<div style="background-color:#fffbe6;border:2px dashed #d4b95e;'
            'padding:16px 20px;margin:26px 0;border-radius:6px;">\n'
            f'<p style="margin:0 0 8px 0;font-weight:bold;color:#8a6d1f;">'
            f'[이미지 {image_number}] 여기에 이미지를 넣고 이 박스는 지우세요</p>\n'
            f'<p style="margin:0 0 6px 0;color:#7a6522;">설명 : {description}</p>\n'
            f'<p style="margin:0;color:#7a6522;">프롬프트 : {prompt}</p>\n'
            '</div>'
        )

    return pattern.sub(replace_image, html)


def _replace_tistory_sections(html: str) -> str:
    toc_pattern = re.compile(
        r"<h2\b[^>]*>(?P<title>(?:(?!</h2>).)*?목차(?:(?!</h2>).)*?)</h2>\s*"
        r"<ol\b[^>]*>(?P<body>.*?)</ol>",
        flags=re.S,
    )

    def wrap_toc(match: re.Match) -> str:
        body = match.group("body").strip()
        return (
            f'<div style="{_TISTORY_TOC_STYLE}">'
            '<p style="margin:0 0 10px 0;font-weight:bold;color:#16324f;">'
            f'{_strip_bold(match.group("title"))}</p>'
            '<ol style="margin:0;padding-left:22px;color:#40566b;">'
            f"{body}</ol></div>\n"
        )

    html = toc_pattern.sub(wrap_toc, html)
    checklist_pattern = re.compile(
        r"<h3\b[^>]*>(?P<title>(?:(?!</h3>).)*?체크리스트(?:(?!</h3>).)*?)</h3>\s*"
        r"<ul\b[^>]*>(?P<body>.*?)</ul>",
        flags=re.S,
    )

    def wrap_checklist(match: re.Match) -> str:
        body = match.group("body").strip()
        return (
            f'<div style="{_TISTORY_CHECKLIST_STYLE}">'
            '<p style="margin:0 0 12px 0;font-weight:bold;color:#2b6b3f;">'
            f'{_strip_bold(match.group("title"))}</p>'
            '<ul style="margin:0;padding-left:22px;color:#33553f;">'
            f"{body}</ul></div>"
        )

    return checklist_pattern.sub(wrap_checklist, html)


def _remove_leading_h1(html: str) -> str:
    return re.sub(r"^\s*<h1\b[^>]*>.*?</h1>\s*", "", html, count=1, flags=re.S | re.I)


def _remove_leading_representative_image(html: str) -> str:
    pattern = re.compile(
        r"^\s*<p\b[^>]*>\s*<img\b[^>]*\bsrc=[\"']data:image/[^\"']+[\"'][^>]*>\s*</p>\s*"
        r"(?:<p\b[^>]*>.*?</p>|<blockquote\b[^>]*>.*?</blockquote>|<div\b[^>]*>.*?</div>)?",
        flags=re.S | re.I,
    )
    return pattern.sub("", html, count=1)


def _remove_platform_metadata_tail(html: str) -> str:
    metadata_h2 = re.search(
        r"<h2\b[^>]*>(?:(?!</h2>).)*(?:내부링크 후보|대표 이미지)(?:(?!</h2>).)*</h2>",
        html,
        flags=re.S,
    )
    return html[: metadata_h2.start()] if metadata_h2 else html


def _replace_tistory_faq_boxes(html: str) -> str:
    pattern = re.compile(
        r"<p\b[^>]*>(Q\..*?)</p>\s*<p\b[^>]*>(A\..*?)</p>",
        flags=re.S,
    )
    faq_number = 0

    def replace_faq(match: re.Match) -> str:
        nonlocal faq_number
        faq_number += 1
        border = "2px solid #2f6fa8" if faq_number == 1 else "1px solid #e3e8ee"
        return (
            f'<div style="border-top:{border};padding-top:16px;margin-top:16px;">\n'
            f'<p style="font-weight:bold;color:#16324f;margin-bottom:6px;">'
            f'{match.group(1).strip()}</p>\n'
            f'<p style="margin-top:0;color:#444444;">{match.group(2).strip()}</p>\n'
            '</div>'
        )

    return pattern.sub(replace_faq, html)


def _attach_tistory_editor_attributes(html: str) -> str:
    attributes = (
        ("p", 'data-ke-size="size16"'),
        ("h2", 'data-ke-size="size26"'),
        ("h3", 'data-ke-size="size23"'),
        ("ul", 'data-ke-list-type="disc"'),
        ("ol", 'data-ke-list-type="decimal"'),
        ("table", 'data-ke-align="alignLeft"'),
    )

    for tag, attribute in attributes:
        def add_attribute(match: re.Match, attribute: str = attribute) -> str:
            opening_tag = match.group(0)
            if re.search(r"\bdata-ke-[\w-]+\s*=", opening_tag):
                return opening_tag
            return f'{opening_tag[:-1]} {attribute}>'

        html = re.sub(rf"<{tag}\b[^>]*>", add_attribute, html)
    return html


def _plain_bold_in_list_items(html: str) -> str:
    """Remove inline colors from bold text inside Tistory list items only."""

    def clear_strong_style(match: re.Match) -> str:
        body = re.sub(
            r"<strong\b[^>]*>(.*?)</strong>",
            r"<strong>\1</strong>",
            match.group(2),
            flags=re.S,
        )
        return f"{match.group(1)}{body}{match.group(3)}"

    return re.sub(
        r"(<li\b[^>]*>)(.*?)(</li>)",
        clear_strong_style,
        html,
        flags=re.S,
    )


def markdown_to_tistory_html(markdown: str) -> str:
    """Convert Markdown to Tistory paste-ready HTML with inline styles only."""
    html = markdown_to_html(markdown, plain=True)
    html = _remove_leading_h1(html)
    html = _remove_leading_representative_image(html)
    html = _remove_platform_metadata_tail(html)
    html = re.sub(r"<hr\b[^>]*/?>", "", html, flags=re.I)
    html = _replace_tistory_image_placeholders(html)
    html = re.sub(
        r"<blockquote\b[^>]*>(.*?)</blockquote>",
        lambda match: f'<div style="{_TISTORY_BLOCKQUOTE_STYLE}">{match.group(1)}</div>',
        html,
        flags=re.S,
    )
    html = _rebuild_tistory_toc(html)
    html = _replace_tistory_table_styles(html)
    html = _replace_tistory_heading_styles(html)
    html = _replace_tistory_callout(
        html,
        "핵심:",
        _TISTORY_CORE_STYLE,
        "color:#1b4f80;font-weight:bold;",
    )
    html = _replace_tistory_callout(
        html,
        "주의:",
        _TISTORY_CAUTION_STYLE,
        "color:#a86a10;font-weight:bold;",
    )
    html = _replace_tistory_callout(
        html,
        "면책:",
        _TISTORY_DISCLAIMER_STYLE,
        "font-weight:bold;",
    )
    html = re.sub(
        r"<strong\b[^>]*>(.*?)</strong>",
        lambda match: f'<strong style="{strong_style(match.group(1))}">{match.group(1)}</strong>',
        html,
        flags=re.S,
    )
    html = _plain_bold_in_list_items(html)
    html = re.sub(
        r"==([^=\n]+)==",
        r'<span style="background-color:#f1f2ca;font-weight:bold;">\1</span>',
        html,
    )
    html = _replace_tistory_faq_boxes(html)
    html = _replace_tistory_sections(html)
    return _attach_tistory_editor_attributes(html)


def _rebuild_tistory_toc(html: str) -> str:
    toc_pattern = re.compile(
        r"(?P<heading><h2\b[^>]*>(?:(?!</h2>).)*?목차(?:(?!</h2>).)*?</h2>)\s*"
        r"(?P<ol><ol\b[^>]*>)(?P<body>.*?)</ol>",
        flags=re.S,
    )
    toc_match = toc_pattern.search(html)
    if toc_match is None:
        return html

    items = []
    heading_pattern = re.compile(r"<h2\b[^>]*>(?P<inner>.*?)</h2>", flags=re.S)
    for heading_match in heading_pattern.finditer(html):
        if heading_match.start() == toc_match.start():
            continue
        title = re.sub(r"<[^>]+>", "", heading_match.group("inner")).strip()
        if title:
            items.append(f"<li>{title}</li>")

    replacement = (
        f'{toc_match.group("heading")}\n'
        f'{toc_match.group("ol")}{"".join(items)}</ol>'
    )
    return html[: toc_match.start()] + replacement + html[toc_match.end() :]


def _strip_bold(text: str) -> str:
    return re.sub(r"</?b>", "", text)


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
