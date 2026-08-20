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


def _card_height(row_count: int) -> int:
    if row_count <= _ROWS_AT_BASE:
        return CARD_HEIGHT_BASE
    extra = (row_count - _ROWS_AT_BASE) * _HEIGHT_PER_EXTRA_ROW
    return min(CARD_HEIGHT_BASE + extra, _MAX_CARD_HEIGHT)


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
.label svg{{width:24px;height:24px}}
h1{{margin-top:26px;font-size:54px;line-height:1.28;font-weight:800;letter-spacing:-.035em}}
"""


# --------------------------------------------------------------------------
# 체크리스트 카드
# --------------------------------------------------------------------------

_CHECK_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="m4 12.5 5 5L20 6.5"/></svg>'
)


def _checklist_html(title: str, items: List[str]) -> str:
    p = _PALETTE
    height = _card_height(len(items))
    rows = "\n".join(
        f'<div class="row"><span class="num">{i}</span>'
        f'<span class="txt">{html.escape(text)}</span>'
        f'<span class="box"></span></div>'
        for i, text in enumerate(items, start=1)
    )
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>
{_base_css(height)}
.items{{margin-top:36px;display:flex;flex-direction:column;gap:16px;flex:1;min-height:0}}
.row{{
  flex:1;display:flex;align-items:center;gap:24px;padding:0 30px;
  background:{p['paper']};border:1px solid {p['line']};border-radius:22px;
  box-shadow:0 4px 14px rgba(18,37,59,.045);
}}
.num{{
  flex:0 0 auto;width:56px;height:56px;border-radius:50%;display:grid;place-items:center;
  font-size:27px;font-weight:800;color:#fff;background:{p['blue']};
}}
.row:nth-child(even) .num{{background:{p['mint']}}}
.txt{{font-size:36px;line-height:1.34;font-weight:700;letter-spacing:-.035em}}
.box{{
  flex:0 0 auto;margin-left:auto;width:38px;height:38px;
  border-radius:9px;border:3px solid #C9D2DA;
}}
</style></head><body>
<span class="label">{_CHECK_ICON}체크</span>
<h1>{html.escape(title)}</h1>
<div class="items">
{rows}
</div>
</body></html>"""


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

_SUPPORTED_STYLES = ("checklist",)

_MIN_ITEMS = 3
_MAX_ITEMS = 10


def render_html_card(spec, output_path: str, *,
                     category_id: str = "", skin=None) -> Optional[str]:
    """HTML 로 카드를 그려 PNG 경로를 돌려준다.

    이 모듈이 감당하지 못하는 모양이거나 뭔가 실패하면 None 을 돌려준다.
    호출부는 None 을 받으면 기존 PIL 렌더러를 쓰면 된다.
    """
    style = getattr(spec, "style", "") or ""
    if style not in _SUPPORTED_STYLES:
        return None

    items = [str(x).strip() for x in (getattr(spec, "items", None) or []) if str(x).strip()]
    if not items:
        items = [
            str(x).strip()
            for x in (getattr(spec, "_checklist_items", None) or [])
            if str(x).strip()
        ]
    if not (_MIN_ITEMS <= len(items) <= _MAX_ITEMS):
        return None

    title = (getattr(spec, "display_title", "") or getattr(spec, "heading", "") or "").strip()
    if not title:
        return None

    markup = _checklist_html(title, items)
    return _render_html_to_png(
        markup, output_path, CARD_WIDTH, _card_height(len(items))
    )