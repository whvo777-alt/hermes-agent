"""HTML + Chromium 방식 인포그래픽 카드 렌더러.

기존 PIL 렌더러(section_infographics.py)를 대체하지 않고 앞에 선다.
이 모듈이 None 을 돌려주면 호출부가 기존 PIL 렌더러로 되돌아간다.

설계 원칙
    1. 실패하면 조용히 None. 예외를 밖으로 던지지 않는다.
    2. 크로미움이 없거나 느리면 None.
    3. PNG 가 실제로 만들어지고 열리는지 확인한 뒤에만 경로를 돌려준다.
"""

from __future__ import annotations

import html
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 크로미움 찾기
# --------------------------------------------------------------------------

_CHROMIUM_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
)

_RENDER_TIMEOUT_SEC = 25


def find_chromium() -> Optional[str]:
    """실행 가능한 크로미움 경로. 없으면 None."""
    for name in _CHROMIUM_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------
# 카드 크기
# --------------------------------------------------------------------------

CARD_WIDTH = 1080
CARD_HEIGHT_BASE = 1350          # 4:5 세로형 기본
_ROWS_AT_BASE = 6                # 이 개수까지는 기본 높이로 충분
_HEIGHT_PER_EXTRA_ROW = 120      # 넘치는 항목 하나당 늘릴 높이
_MAX_CARD_HEIGHT = 2400


_HEADER_H = 250        # 위 여백 + 라벨 + 제목
_FOOTER_H = 58         # 아래 여백
_ROW_GAP = 15
_MIN_CARD_HEIGHT = 700


def _stack_height(row_count: int, row_h: int = 150, extra: int = 0) -> int:
    """줄 수에 맞춰 카드 높이를 정한다."""
    rows = max(1, int(row_count))
    total = _HEADER_H + rows * row_h + (rows - 1) * _ROW_GAP + extra + _FOOTER_H + 40
    return max(_MIN_CARD_HEIGHT, min(total, _MAX_CARD_HEIGHT))


def _card_height(row_count: int) -> int:
    """옛 이름 유지."""
    return _stack_height(row_count)


# --------------------------------------------------------------------------
# 색
# --------------------------------------------------------------------------

_PALETTE = {
    "ivory": "#F7F4EE",
    "paper": "#FFFFFF",
    "navy": "#12253B",
    "muted": "#6B7A88",
    "blue": "#3678AD",
    "mint": "#5FB9A2",
    "blue_pale": "#E6EFF7",
    "mint_pale": "#E3F2EC",
    "coral": "#D97757",
    "coral_pale": "#FBEDE7",
    "line": "#E4E1DA",
}

_FONT_STACK = (
    '"NanumSquareRound","NanumSquare","NanumBarunGothic",'
    '"NanumGothic","Noto Sans CJK KR",sans-serif'
)


def _base_css(height: int) -> str:
    p = _PALETTE
    return f"""
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{width:{CARD_WIDTH}px;height:{height}px}}
body{{
  background:{p['ivory']};color:{p['navy']};font-family:{_FONT_STACK};
  padding:60px 58px 58px;display:flex;flex-direction:column;
  word-break:keep-all;-webkit-font-smoothing:antialiased;
}}
.label{{
  display:inline-flex;align-items:center;gap:12px;align-self:flex-start;
  padding:11px 22px 12px;border-radius:999px;background:{p['blue']};color:#fff;
  font-size:24px;font-weight:800;letter-spacing:-.02em;
}}
.label.warn{{background:#D97757}}
.label svg{{width:24px;height:24px}}
h1{{margin-top:26px;font-size:54px;line-height:1.28;font-weight:800;letter-spacing:-.035em}}
"""


# --------------------------------------------------------------------------
# 아이콘
# --------------------------------------------------------------------------

def _svg(path: str, width: float = 2.6) -> str:
    return (
        f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">'
        f'{path}</svg>'
    )


_ICON_CHECK = _svg('<path d="m4 12.5 5 5L20 6.5"/>')
_ICON_DOWN = _svg('<path d="M12 4v16M6 14l6 6 6-6"/>', 2.4)
_ICON_SWAP = _svg('<path d="M8 7H3m0 0 3-3M3 7l3 3M16 17h5m0 0-3-3m3 3-3 3"/>', 2.4)
_ICON_QA = _svg('<path d="M9.3 8.4a3 3 0 1 1 5.1 2.2c-1.1 1-2.3 1.4-2.3 3M12 17.2v.1"/>'
                '<circle cx="12" cy="12" r="9"/>', 2.1)
_ICON_QUOTE = _svg('<path d="M9 7H5.5A1.5 1.5 0 0 0 4 8.5V12h5V7Zm0 0v3c0 4-2 6-5 7"/>'
                   '<path d="M20 7h-3.5A1.5 1.5 0 0 0 15 8.5V12h5V7Zm0 0v3c0 4-2 6-5 7"/>', 2.1)
_ICON_WARN = _svg('<path d="M12 9v4M12 17h.01M10.3 3.9 2.4 17.6A2 2 0 0 0 4.1 20.6h15.8'
                  'a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>', 2.2)
_ICON_TABLE = _svg('<rect x="3.5" y="4.5" width="17" height="15" rx="2"/>'
                   '<path d="M3.5 10h17M9.5 10v9.5"/>', 2.1)
_ICON_ARROW = _svg('<path d="M5 12h14m0 0-5-5m5 5-5 5"/>')


# --------------------------------------------------------------------------
# 공통 조각
# --------------------------------------------------------------------------

def _page(css: str, label_html: str, title: str, body: str, height: int) -> str:
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>'
        + _base_css(height) + css +
        '</style></head><body>'
        + label_html
        + f'<h1>{html.escape(title)}</h1>'
        + body
        + '</body></html>'
    )


def _label(icon: str, text: str, warn: bool = False) -> str:
    cls = "label warn" if warn else "label"
    return f'<span class="{cls}">{icon}{html.escape(text)}</span>'


_ROWS_CSS = """
.rows{{margin-top:34px;display:flex;flex-direction:column;gap:15px;flex:1;min-height:0}}
.rw{{
  flex:1;display:flex;align-items:center;gap:24px;padding:0 30px;
  background:{paper};border:1px solid {line};border-radius:22px;
  box-shadow:0 4px 14px rgba(18,37,59,.045);
}}
"""


# --------------------------------------------------------------------------
# 체크리스트 카드
# --------------------------------------------------------------------------

def _checklist_html(title: str, items: List[str]) -> str:
    p = _PALETTE
    height = _card_height(len(items))
    rows = "".join(
        f'<div class="rw"><span class="num">{i}</span>'
        f'<span class="txt">{html.escape(t)}</span><span class="box"></span></div>'
        for i, t in enumerate(items, start=1)
    )
    css = _ROWS_CSS.format(**p) + f"""
.num{{flex:0 0 auto;width:56px;height:56px;border-radius:50%;display:grid;place-items:center;
  font-size:27px;font-weight:800;color:#fff;background:{p['blue']}}}
.rw:nth-child(even) .num{{background:{p['mint']}}}
.txt{{font-size:36px;line-height:1.34;font-weight:700;letter-spacing:-.035em}}
.box{{flex:0 0 auto;margin-left:auto;width:38px;height:38px;border-radius:9px;border:3px solid #C9D2DA}}
"""
    return _page(css, _label(_ICON_CHECK, "체크"), title,
                 f'<div class="rows">{rows}</div>', height)


# --------------------------------------------------------------------------
# 단계 카드
# --------------------------------------------------------------------------

def _timeline_html(title: str, items: List[str]) -> str:
    p = _PALETTE
    height = _card_height(len(items))
    rows = "".join(
        f'<div class="rw"><span class="num">{i}</span>'
        f'<span class="txt">{html.escape(t)}</span></div>'
        for i, t in enumerate(items, start=1)
    )
    css = _ROWS_CSS.format(**p) + f"""
.rw{{position:relative}}
.rw::after{{content:"";position:absolute;left:92px;bottom:-15px;width:2px;height:15px;background:#CBD6DE}}
.rw:last-child::after{{display:none}}
.num{{flex:0 0 auto;width:62px;height:62px;border-radius:20px;display:grid;place-items:center;
  font-size:26px;font-weight:800;color:#fff;background:{p['blue']}}}
.rw:nth-child(even) .num{{background:{p['mint']}}}
.txt{{font-size:35px;line-height:1.3;font-weight:700;letter-spacing:-.035em}}
"""
    return _page(css, _label(_ICON_DOWN, "단계"), title,
                 f'<div class="rows">{rows}</div>', height)


# --------------------------------------------------------------------------
# 비교표 카드
# --------------------------------------------------------------------------

def _grid_html(title: str, table: List[List[str]]) -> str:
    p = _PALETTE
    header, body = table[0], table[1:]
    height = _stack_height(len(body), row_h=118, extra=86)
    ths = "".join(f"<th>{html.escape(str(c))}</th>" for c in header)
    trs = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>"
        for row in body
    )
    css = f"""
.wrap{{margin-top:34px;flex:1;min-height:0;background:{p['paper']};border:1px solid {p['line']};
  border-radius:24px;overflow:hidden;box-shadow:0 4px 14px rgba(18,37,59,.045);display:flex}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}}
th{{background:{p['blue_pale']};color:{p['blue']};font-size:29px;font-weight:800;
  padding:24px 22px;text-align:left;letter-spacing:-.03em}}
td{{font-size:30px;font-weight:700;padding:22px;letter-spacing:-.035em;line-height:1.32;
  border-top:1px solid {p['line']};vertical-align:middle}}
tr td:first-child{{color:{p['blue']};font-weight:800}}
"""
    return _page(css, _label(_ICON_TABLE, "비교"), title,
                 f'<div class="wrap"><table><thead><tr>{ths}</tr></thead>'
                 f'<tbody>{trs}</tbody></table></div>', height)


# --------------------------------------------------------------------------
# 전후 비교 카드
# --------------------------------------------------------------------------

def _before_after_html(title: str, pairs) -> str:
    p = _PALETTE
    height = _stack_height(len(pairs), row_h=132, extra=72)
    rows = "".join(
        f'<div class="pair"><div class="cell l">{html.escape(str(b))}</div>'
        f'<span class="arw">{_ICON_ARROW}</span>'
        f'<div class="cell r">{html.escape(str(a))}</div></div>'
        for b, a in pairs
    )
    css = f"""
.cmp{{margin-top:30px;flex:1;min-height:0;display:flex;flex-direction:column;gap:14px}}
.head{{display:flex;gap:16px;flex:0 0 auto}}
.head div{{flex:1;padding:14px 0;text-align:center;border-radius:16px;font-size:27px;
  font-weight:800;letter-spacing:-.03em}}
.head .l{{background:#EFEDE9;color:#8E9AA5}}
.head .r{{background:{p['mint_pale']};color:#2E7D68}}
.pair{{flex:1;display:flex;align-items:center;gap:16px}}
.cell{{flex:1;height:100%;display:flex;align-items:center;justify-content:center;text-align:center;
  padding:0 18px;border-radius:20px;font-size:29px;font-weight:700;letter-spacing:-.035em;line-height:1.28;
  background:{p['paper']}}}
.cell.l{{border:1px solid {p['line']};color:#8E9AA5}}
.cell.r{{border:2px solid #BFE3D7;color:{p['navy']}}}
.arw{{flex:0 0 auto;width:36px;height:36px;color:{p['mint']}}}
.arw svg{{width:36px;height:36px}}
"""
    body = ('<div class="cmp"><div class="head"><div class="l">이렇게 하기 쉽습니다</div>'
            '<div class="r">이렇게 바꿔보세요</div></div>' + rows + '</div>')
    return _page(css, _label(_ICON_SWAP, "비교 전/후"), title, body, height)


# --------------------------------------------------------------------------
# Q&A 카드
# --------------------------------------------------------------------------

def _qa_html(title: str, pairs) -> str:
    p = _PALETTE
    height = _stack_height(len(pairs), row_h=280, extra=20)
    blocks = "".join(
        f'<div class="qa"><div class="q"><span class="mk">Q</span>'
        f'<span>{html.escape(str(q))}</span></div>'
        f'<div class="a"><span class="mk am">A</span>'
        f'<span>{html.escape(str(a))}</span></div></div>'
        for q, a in pairs
    )
    css = f"""
.list{{margin-top:34px;flex:1;min-height:0;display:flex;flex-direction:column;gap:20px}}
.qa{{flex:1;display:flex;flex-direction:column;justify-content:center;gap:16px;padding:26px 30px;
  background:{p['paper']};border:1px solid {p['line']};border-radius:24px;
  box-shadow:0 4px 14px rgba(18,37,59,.045)}}
.q,.a{{display:flex;gap:18px;align-items:flex-start}}
.mk{{flex:0 0 auto;width:46px;height:46px;border-radius:14px;display:grid;place-items:center;
  font-size:24px;font-weight:800;color:#fff;background:{p['blue']}}}
.mk.am{{background:{p['mint']}}}
.q span:last-child{{font-size:32px;font-weight:800;letter-spacing:-.035em;line-height:1.32;padding-top:4px}}
.a span:last-child{{font-size:27px;font-weight:700;color:{p['muted']};letter-spacing:-.03em;line-height:1.45;padding-top:8px}}
"""
    return _page(css, _label(_ICON_QA, "Q&A"), title,
                 f'<div class="list">{blocks}</div>', height)


# --------------------------------------------------------------------------
# 인용 카드
# --------------------------------------------------------------------------

def _quote_html(title: str, quote: str, keyword: bool = False) -> str:
    p = _PALETTE
    height = _stack_height(1, row_h=max(230, 90 + len(quote) * 3))
    css = f"""
.qbox{{margin-top:34px;flex:1;min-height:0;display:flex;align-items:center;gap:28px;
  padding:44px 46px;background:{p['paper']};border:1px solid {p['line']};border-radius:26px;
  box-shadow:0 4px 14px rgba(18,37,59,.045)}}
.bar{{flex:0 0 auto;width:8px;align-self:stretch;border-radius:99px;
  background:linear-gradient(180deg,{p['blue']},{p['mint']})}}
.qbox p{{font-size:40px;line-height:1.5;font-weight:800;letter-spacing:-.04em}}
"""
    label = _label(_ICON_QUOTE, "포인트" if keyword else "핵심")
    body = f'<div class="qbox"><span class="bar"></span><p>{html.escape(quote)}</p></div>'
    return _page(css, label, title, body, height)


# --------------------------------------------------------------------------
# 등급 비교 카드
# --------------------------------------------------------------------------

_TIER_COLORS = {
    "safe": ("#2E7D68", "#E3F2EC"),
    "ok": ("#2E7D68", "#E3F2EC"),
    "low": ("#2E7D68", "#E3F2EC"),
    "caution": ("#B4791F", "#FBF1DE"),
    "warn": ("#B4791F", "#FBF1DE"),
    "mid": ("#B4791F", "#FBF1DE"),
    "danger": ("#D97757", "#FBEDE7"),
    "high": ("#D97757", "#FBEDE7"),
}


def _risk_tier_html(title: str, tiers) -> str:
    p = _PALETTE
    height = _stack_height(len(tiers), row_h=155)
    rows = []
    for group, label, desc in tiers:
        fg, bg = _TIER_COLORS.get(str(group).lower(), (p["blue"], p["blue_pale"]))
        rows.append(
            f'<div class="rw"><span class="tag" style="color:{fg};background:{bg}">'
            f'{html.escape(str(label))}</span>'
            f'<span class="txt">{html.escape(str(desc))}</span></div>'
        )
    css = _ROWS_CSS.format(**p) + """
.tag{flex:0 0 auto;min-width:150px;padding:14px 20px;border-radius:16px;text-align:center;
  font-size:27px;font-weight:800;letter-spacing:-.03em}
.txt{font-size:31px;line-height:1.32;font-weight:700;letter-spacing:-.035em}
"""
    return _page(css, _label(_ICON_WARN, "등급 비교", warn=True), title,
                 f'<div class="rows">{"".join(rows)}</div>', height)


# --------------------------------------------------------------------------
# 렌더링
# --------------------------------------------------------------------------

def _run_chromium(chromium: str, html_path: str, png_path: str,
                  width: int, height: int) -> bool:
    """크로미움으로 HTML 을 PNG 로 찍는다. 성공하면 True."""
    with tempfile.TemporaryDirectory(prefix="hermes-chrome-") as profile_dir:
        cmd = [
            chromium,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile_dir}",
            f"--window-size={width},{height}",
            f"--screenshot={png_path}",
            f"file://{html_path}",
        ]
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_RENDER_TIMEOUT_SEC,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _LOG.warning("chromium screenshot failed: %s", exc)
            return False
    return True


def _png_is_valid(png_path: str, width: int, height: int) -> bool:
    """PNG 가 실제로 만들어졌고 크기가 맞는지 확인."""
    path = Path(png_path)
    if not path.is_file() or path.stat().st_size < 1000:
        return False
    try:
        from PIL import Image  # noqa: PLC0415 — 실패해도 되는 선택적 검증
        with Image.open(path) as img:
            return img.size == (width, height)
    except Exception:  # noqa: BLE001 — 열리지 않으면 실패로 본다
        return False


def _render_html_to_png(markup: str, output_path: str,
                        width: int, height: int) -> Optional[str]:
    chromium = find_chromium()
    if not chromium:
        return None

    out = Path(output_path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    tmp_html = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", encoding="utf-8", delete=False
        ) as handle:
            handle.write(markup)
            tmp_html = handle.name

        if not _run_chromium(chromium, tmp_html, str(out), width, height):
            return None
        if not _png_is_valid(str(out), width, height):
            _LOG.warning("chromium produced an unusable png: %s", out)
            return None
        return str(out)
    except Exception as exc:  # noqa: BLE001 — 렌더 실패는 절대 위로 던지지 않는다
        _LOG.warning("html card render failed: %s", exc)
        return None
    finally:
        if tmp_html:
            try:
                os.unlink(tmp_html)
            except OSError:
                pass


# --------------------------------------------------------------------------
# 바깥에서 부르는 함수
# --------------------------------------------------------------------------

_MIN_ITEMS = 3
_MAX_ITEMS = 10


def _clean(values) -> List[str]:
    return [str(v).strip() for v in (values or []) if str(v).strip()]


def _pairs(values):
    out = []
    for pair in (values or []):
        try:
            left, right = str(pair[0]).strip(), str(pair[1]).strip()
        except (TypeError, IndexError):
            continue
        if left and right:
            out.append((left, right))
    return out


def _build_markup(spec, style: str) -> Optional[str]:
    """스펙을 HTML 문자열로. 감당 못 하는 모양이면 None."""
    title = (
        getattr(spec, "display_title", "") or getattr(spec, "heading", "") or ""
    ).strip()
    if not title:
        return None

    if style in ("checklist", "timeline"):
        items = _clean(getattr(spec, "items", None))
        if not items:
            items = _clean(getattr(spec, "_checklist_items", None))
        if not (_MIN_ITEMS <= len(items) <= _MAX_ITEMS):
            return None
        if style == "checklist":
            return _checklist_html(title, items)
        return _timeline_html(title, items)

    if style == "grid":
        table = getattr(spec, "table", None) or []
        rows = [[str(c).strip() for c in row] for row in table if row]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2 or len(rows) > 9:
            return None
        if len(rows[0]) < 2 or len(rows[0]) > 4:
            return None
        return _grid_html(title, rows)

    if style == "before_after":
        pairs = _pairs(getattr(spec, "before_pairs", None))
        if not (2 <= len(pairs) <= 7):
            return None
        return _before_after_html(title, pairs)

    if style == "qa":
        pairs = _pairs(getattr(spec, "qa_pairs", None))
        if not (1 <= len(pairs) <= 3):
            return None
        return _qa_html(title, pairs)

    if style in ("quote", "quote_keyword"):
        quote = str(getattr(spec, "quote_text", "") or "").strip()
        if not (10 <= len(quote) <= 160):
            return None
        return _quote_html(title, quote, keyword=(style == "quote_keyword"))

    if style == "risk_tier":
        tiers = []
        for tier in (getattr(spec, "risk_tiers", None) or []):
            try:
                group, label, desc = tier[0], tier[1], tier[2]
            except (TypeError, IndexError):
                continue
            if str(label).strip() and str(desc).strip():
                tiers.append((group, str(label).strip(), str(desc).strip()))
        if not (2 <= len(tiers) <= 6):
            return None
        return _risk_tier_html(title, tiers)

    return None


def _markup_height(markup: str) -> int:
    """만들어진 HTML 에서 body 높이를 읽어낸다."""
    import re

    found = re.search(r"html,body\{width:\d+px;height:(\d+)px\}", markup)
    return int(found.group(1)) if found else CARD_HEIGHT_BASE


def render_html_card(spec, output_path: str, *,
                     category_id: str = "", skin=None) -> Optional[str]:
    """HTML 로 카드를 그려 PNG 경로를 돌려준다.

    이 모듈이 감당하지 못하는 모양이거나 뭔가 실패하면 None 을 돌려준다.
    호출부는 None 을 받으면 기존 PIL 렌더러를 쓰면 된다.
    """
    style = str(getattr(spec, "style", "") or "")
    markup = _build_markup(spec, style)
    if not markup:
        return None
    return _render_html_to_png(
        markup, output_path, CARD_WIDTH, _markup_height(markup)
    )