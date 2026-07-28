"""Shared text-measurement helpers for PIL-drawn cards (title_card.py,
section_infographics.py). Everything here measures REAL rendered pixel
width (``draw.textlength``) rather than a character-count budget, and never
hard-clips text without an ellipsis fallback.
"""

from __future__ import annotations

from typing import Callable, List, Tuple


def ellipsize(draw, text: str, font, max_width: int) -> str:
    """Return ``text`` unchanged if it already fits ``max_width``, otherwise
    trim it (character by character) and append "…" so it fits."""
    text = text or ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed.rstrip() + "…") if trimmed else "…"


def _greedy_wrap_all(draw, text: str, font, max_width: int) -> List[str]:
    """Word-wrap the FULL text with no line-count limit — used so callers can
    tell whether a max_lines cap actually dropped content."""
    words = (text or "").split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def wrap_text(draw, text: str, font, max_width: int, max_lines: int) -> List[str]:
    """Greedy word-wrap using real measured pixel width, capped at
    ``max_lines``. Any line that still doesn't fit ``max_width`` (a single
    word wider than the box) is ellipsized -- this can happen on ANY line,
    not just the last. If the text needs more than ``max_lines`` lines, the
    last kept line is ellipsized too, so truncation is always visible
    instead of silently dropping the remainder mid-word."""
    all_lines = _greedy_wrap_all(draw, text, font, max_width)
    truncated = len(all_lines) > max_lines
    lines = all_lines[:max_lines]
    lines = [ellipsize(draw, line, font, max_width) for line in lines]
    if truncated and lines and not lines[-1].endswith("…"):
        lines[-1] = ellipsize(draw, lines[-1] + "…", font, max_width)
    return lines


def _wrap_all_fits(draw, text: str, font, max_width: int, max_lines: int) -> bool:
    """True if ``text`` wraps into <= max_lines lines at this font size with
    every line fitting max_width -- i.e. no ellipsis would be needed."""
    all_lines = _greedy_wrap_all(draw, text, font, max_width)
    if len(all_lines) > max_lines:
        return False
    return all(draw.textlength(line, font=font) <= max_width for line in all_lines)


def fit_font_and_wrap(
    draw, text: str, *,
    font_loader: Callable[[int], object],
    base_size: int, min_size: int, step: int,
    max_width: int, max_lines: int,
) -> Tuple[List[str], object]:
    """Try ``base_size``, then step down by ``step`` (bounded by ``min_size``)
    until ``text`` wraps into ``max_lines`` lines with no line overflowing
    ``max_width`` -- i.e. box/font size adapts to the actual measured text
    instead of a fixed size that can clip. Only falls back to ellipsis
    truncation (via ``wrap_text``) once ``min_size`` is reached and it still
    doesn't fit."""
    size = base_size
    while True:
        font = font_loader(size)
        if _wrap_all_fits(draw, text, font, max_width, max_lines) or size <= min_size:
            return wrap_text(draw, text, font, max_width, max_lines), font
        size -= step
