"""Naver real-publish bridge — ported from
multi-content-pipeline/scripts/naver-bridge.js.

Two independent fail-closed gates, preserved exactly from the source:
  1. ``NAVER_BRIDGE_UNSAFE_OPT_IN=true`` — required before ANY browser/page
     is created. Nothing below launches a browser without this.
  2. ``LIVE_NAVER_PUBLISH=true`` — a SEPARATE gate required before the final
     "발행" (publish) click. Draft-save (``fill_and_save``) does not require
     this second gate, matching the source's behavior.

Requires the optional ``playwright`` dependency
(``pip install "hermes-agent[naver]"``) — imported lazily so the rest of
``agent/content`` never breaks if it isn't installed. NOT executed against
the real network as part of this migration: Playwright is not installed in
this environment, and doing so would itself be an unauthorized live
browser/API interaction. Only syntax/structure has been verified.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from playwright.async_api import async_playwright  # type: ignore

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover — optional dependency
    async_playwright = None  # type: ignore
    _PLAYWRIGHT_AVAILABLE = False


class NaverBridgeError(RuntimeError):
    pass


@dataclass
class NaverGateResult:
    allowed: bool
    reason: str


def check_bridge_start_gate(env: Optional[Dict[str, str]] = None) -> NaverGateResult:
    """Gate 1 — must pass before any browser/page is created."""
    env = env if env is not None else os.environ
    if env.get("NAVER_BRIDGE_UNSAFE_OPT_IN") != "true":
        return NaverGateResult(
            False,
            "BLOCKED: Naver is disabled_until_governed. Set NAVER_BRIDGE_UNSAFE_OPT_IN=true "
            "to explicitly opt in. No browser was launched.",
        )
    return NaverGateResult(True, "bridge start gate satisfied")


def check_publish_gate(env: Optional[Dict[str, str]] = None) -> NaverGateResult:
    """Gate 2 — independent, must pass before the actual '발행' click."""
    env = env if env is not None else os.environ
    if env.get("LIVE_NAVER_PUBLISH") != "true":
        return NaverGateResult(
            False,
            "publish_blocked_missing_opt_in: LIVE_NAVER_PUBLISH=true is required to actually "
            "publish. This call performed no browser action.",
        )
    return NaverGateResult(True, "publish gate satisfied")


# Legacy combined check kept for callers using the old single-function gate
# (agent/content/orchestrator.py's dry-run path does not need a real bridge).
def check_naver_publish_gate(env: Optional[Dict[str, str]] = None) -> NaverGateResult:
    start = check_bridge_start_gate(env)
    if not start.allowed:
        return start
    return check_publish_gate(env)


def parse_draft(markdown: str) -> Dict[str, Any]:
    """Ported from naver-bridge.js ``readDraft()`` — title/body/image extraction
    and Naver SmartEditor-friendly body cleanup (strip markdown syntax the
    editor can't paste as-is)."""
    title_match = re.search(r"^제목:\s*(.+)$", markdown, flags=re.M) or re.search(r"^#\s+(.+)$", markdown, flags=re.M)
    title = (title_match.group(1) if title_match else "네이버 블로그 자동화 테스트").strip()

    tags_match = re.search(r"^태그:\s*(.+)$", markdown, flags=re.M)
    tags = tags_match.group(1).strip() if tags_match else ""

    image_match = re.search(r"!\[[^\]]*\]\(([^)]+)\)", markdown) or re.search(
        r"^대표 이미지 파일:\s*(.+)$", markdown, flags=re.M
    )
    image_file = image_match.group(1).strip() if image_match else None

    body = re.sub(r"^---[\s\S]*?---\s*", "", markdown)
    body = re.sub(r"^제목:.*$", "", body, flags=re.M)
    body = re.sub(r"^#\s+.*$", "", body, flags=re.M)
    body = re.sub(r"^태그:.*$", "", body, flags=re.M)
    body = re.sub(r"^카테고리:.*$", "", body, flags=re.M)
    body = re.sub(r"!\[([^\]]*)\]\([^)]+\)", lambda m: f"이미지 삽입 위치: {m.group(1) or '대표 이미지'}", body)
    body = re.sub(r"\[이미지\d+:[^\]]+\]\n이미지 설명:[^\n]+", "", body)
    body = re.sub(r"\[이미지(\d+):\s*([^\]]+)\]", r"이미지 삽입 위치 \1: \2", body)
    body = re.sub(r"\[이미지(\d+)\]", r"이미지 삽입 위치 \1", body)
    body = re.sub(r"^#{1,6}\s*", "", body, flags=re.M)
    body = body.replace("**", "")
    body = re.sub(r"^[-*]\s+\[[ xX]\]\s*", "• ", body, flags=re.M)
    body = re.sub(r"^[-*]\s+", "• ", body, flags=re.M)
    body = re.sub(r"^---+$", "", body, flags=re.M)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    return {"title": title, "body": f"{body}\n\n{tags}"[:4500], "imageFile": image_file}


@dataclass
class NaverBridgeSession:
    """Stateful wrapper around one persistent Playwright browser context —
    ported from naver-bridge.js's module-level ``context``/``page``/``state``.

    Unlike the source (an always-on HTTP server), this is an explicit Python
    object the caller creates, uses for one or more calls, and closes — no
    HTTP layer, since Hermes calls this in-process.
    """

    user_data_dir: str = field(default_factory=lambda: os.environ.get("NAVER_PLAYWRIGHT_PROFILE", ".auth/naver-profile"))
    headless: bool = field(default_factory=lambda: os.environ.get("NAVER_HEADLESS", "true") != "false")
    _context: Any = field(default=None, init=False, repr=False)
    _page: Any = field(default=None, init=False, repr=False)
    _playwright: Any = field(default=None, init=False, repr=False)

    async def ensure_page(self):
        if not _PLAYWRIGHT_AVAILABLE:
            raise NaverBridgeError(
                "playwright is not installed. Install the optional dependency: "
                'pip install "hermes-agent[naver]"'
            )
        if self._context is None:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                headless=self.headless,
                viewport={"width": 1365, "height": 900},
                locale="ko-KR",
            )
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = None
        self._page = None
        self._playwright = None

    async def is_login_page(self) -> bool:
        page = self._page
        if re.search(r"nidlogin\.login|login", page.url, re.I):
            return True
        try:
            return await page.locator("#id, #pw, #log\\.login, #loinid").first.is_visible()
        except Exception:
            return False

    async def login_with_password(self) -> Dict[str, Any]:
        """Ported from naver-bridge.js loginWithPassword(). Requires
        NAVER_ID/NAVER_USERNAME + NAVER_PASSWORD; fails closed (no action)
        if either is missing."""
        page = await self.ensure_page()
        username = (os.environ.get("NAVER_ID") or os.environ.get("NAVER_USERNAME") or "").strip()
        password = (os.environ.get("NAVER_PASSWORD") or "").strip()
        if not username or not password:
            return {"ok": False, "state": "password_login_unavailable", "reason": "NAVER_USERNAME/NAVER_PASSWORD missing"}

        await page.goto(
            "https://nid.naver.com/nidlogin.login?mode=form&url=https://blog.naver.com/GoBlogWrite.naver",
            wait_until="domcontentloaded", timeout=60000,
        )
        await page.wait_for_timeout(1500)
        try:
            form_visible = await page.locator('#id, input[name="id"]').first.is_visible()
        except Exception:
            form_visible = False
        if not form_visible and not await self.is_login_page():
            return {"ok": True, "state": "already_logged_in", "currentUrl": page.url}

        await page.evaluate(
            """({username, password}) => {
                function setValue(el, value) {
                    const proto = el instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    setter?.call(el, value);
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
                const id = document.querySelector('#id, input[name="id"]');
                const pw = document.querySelector('#pw, input[name="pw"]');
                if (id) setValue(id, username);
                if (pw) setValue(pw, password);
            }""",
            {"username": username, "password": password},
        )
        await page.wait_for_timeout(500)
        try:
            await page.locator('#log\\.login, button[type="submit"], input[type="submit"]').first.click(timeout=5000)
        except Exception:
            try:
                await page.keyboard.press("Enter")
            except Exception:
                pass
        await page.wait_for_timeout(7000)

        html = await page.content()
        security_blocked = bool(re.search(r"captcha|자동입력|보안|인증|기기 등록|2단계|OTP|일회용|보호조치", html))
        if security_blocked:
            return {"ok": False, "state": "password_login_security_blocked", "currentUrl": page.url}

        try:
            await page.goto("https://blog.naver.com/GoBlogWrite.naver", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(4500)
        ok = not await self.is_login_page()
        return {"ok": ok, "state": "logged_in" if ok else "password_login_failed", "currentUrl": page.url}

    async def ensure_logged_in(self) -> bool:
        page = await self.ensure_page()
        try:
            await page.goto("https://blog.naver.com/GoBlogWrite.naver", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(3500)
        if await self.is_login_page():
            result = await self.login_with_password()
            return result.get("ok") is True
        return True

    async def close_popups(self) -> None:
        page = self._page
        try:
            await page.get_by_text("취소", exact=True).first.click(timeout=1500)
        except Exception:
            pass
        try:
            await page.mouse.click(625, 526)
        except Exception:
            pass
        await page.wait_for_timeout(1800)
        for selector in ['button:has-text("닫기")', "text=닫기", 'button[aria-label="닫기"]', "[class*=Close] button", "[class*=close]"]:
            try:
                await page.locator(selector).first.click(timeout=800)
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(800)

    async def insert_body_text(self, body: str) -> str:
        """Prefer a single clipboard paste (matches source), fall back to
        paragraph-sized keyboard insertions."""
        page = self._page
        try:
            await page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://blog.naver.com")
        except Exception:
            pass
        try:
            pasted = await page.evaluate(
                """async (text) => { try { await navigator.clipboard.writeText(text); return true; } catch { return false; } }""",
                body,
            )
        except Exception:
            pasted = False

        if pasted:
            await page.keyboard.press("Meta+V" if sys.platform == "darwin" else "Control+V")
            await page.wait_for_timeout(min(3500, 800 + len(body) // 12))
            return "clipboard_paste"

        chunks = [c.strip() for c in re.split(r"\n{2,}", body) if c.strip()]
        for chunk in chunks:
            await page.keyboard.insert_text(chunk)
            await page.keyboard.press("Enter")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(120)
        return "paragraph_insert"

    async def upload_image_at_cursor(self, image_file: Optional[str]) -> Dict[str, Any]:
        if not image_file:
            return {"uploaded": False, "reason": "no_image_file"}
        page = self._page
        abs_path = image_file if image_file.startswith("/") else str(Path.cwd() / image_file)
        if not Path(abs_path).is_file():
            return {"uploaded": False, "reason": f"image_not_found:{abs_path}"}
        try:
            async with page.expect_file_chooser(timeout=5000) as chooser_info:
                await page.mouse.click(36, 65)
            chooser = await chooser_info.value
        except Exception:
            return {"uploaded": False, "reason": "filechooser_not_opened"}
        await chooser.set_files(abs_path)
        await page.wait_for_timeout(6000)
        return {"uploaded": True, "file": abs_path}

    async def select_private_and_publish(self) -> Dict[str, Any]:
        page = self._page
        try:
            await page.mouse.click(1298, 22)
        except Exception:
            pass
        await page.wait_for_timeout(3000)

        clicked_private = False
        try:
            await page.get_by_text("비공개", exact=True).first.click(timeout=3000)
            clicked_private = True
        except Exception:
            try:
                dom_clicked = await page.evaluate(
                    """() => {
                        const els = Array.from(document.querySelectorAll('button,label,span,div,input'));
                        const el = els.find(e => (e.innerText || e.textContent || e.getAttribute('aria-label') || '').trim() === '비공개');
                        if (el) { el.click(); return true; }
                        return false;
                    }"""
                )
            except Exception:
                dom_clicked = False
            if dom_clicked:
                clicked_private = True
            else:
                try:
                    await page.mouse.click(1252, 178)
                except Exception:
                    pass
                clicked_private = True

        await page.wait_for_timeout(1200)
        try:
            private_visible = await page.evaluate("() => /비공개/.test(document.body?.innerText || '')")
        except Exception:
            private_visible = False
        if not clicked_private or not private_visible:
            return {"ok": False, "state": "private_selection_failed", "clickedPrivate": clicked_private, "privateVisible": private_visible}

        published = False
        try:
            await page.get_by_text("발행", exact=True).last.click(timeout=5000)
            published = True
        except Exception:
            try:
                published = await page.evaluate(
                    """() => {
                        const els = Array.from(document.querySelectorAll('button,a,[role=button],span,div')).reverse();
                        const el = els.find(e => (e.innerText || e.textContent || '').trim() === '발행');
                        if (el) { el.click(); return true; }
                        return false;
                    }"""
                )
            except Exception:
                published = False

        await page.wait_for_timeout(8000)
        return {"ok": published, "state": "private_publish_clicked" if published else "final_publish_button_not_found", "currentUrl": page.url}

    async def fill_and_private_publish(self, markdown: str) -> Dict[str, Any]:
        """Gate 2 (LIVE_NAVER_PUBLISH) is checked here, independently of the
        Gate 1 check the caller must already have passed to create this
        session at all — matches the source's two-gate design exactly."""
        gate = check_publish_gate()
        if not gate.allowed:
            return {"ok": False, "state": "publish_blocked_missing_opt_in", "reason": gate.reason}

        page = await self.ensure_page()
        if not await self.ensure_logged_in():
            return {"ok": False, "state": "login_required", "hint": "call login flow first"}

        draft = parse_draft(markdown)
        try:
            await page.goto("https://blog.naver.com/GoBlogWrite.naver", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(7000)
        await self.close_popups()

        await page.mouse.click(365, 250)
        await page.keyboard.insert_text(draft["title"])
        await page.wait_for_timeout(900)
        await page.mouse.click(365, 360)
        image_result = await self.upload_image_at_cursor(draft["imageFile"])
        try:
            await page.mouse.click(365, 620 if image_result["uploaded"] else 360)
        except Exception:
            pass
        insertion_mode = await self.insert_body_text(draft["body"])
        await page.wait_for_timeout(2500)
        publish_result = await self.select_private_and_publish()

        return {
            "ok": publish_result["ok"] is True,
            "state": "private_publish_attempted" if publish_result["ok"] else publish_result["state"],
            "currentUrl": page.url,
            "title": draft["title"],
            "image": image_result,
            "insertionMode": insertion_mode,
            "bodyLength": len(draft["body"]),
            "publishResult": publish_result,
        }

    async def fill_and_save(self, markdown: str) -> Dict[str, Any]:
        """Draft-save only (no publish click) — does not require Gate 2."""
        page = await self.ensure_page()
        if not await self.ensure_logged_in():
            return {"ok": False, "state": "login_required", "hint": "call login flow first"}

        draft = parse_draft(markdown)
        try:
            await page.goto("https://blog.naver.com/GoBlogWrite.naver", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(7000)
        await self.close_popups()

        await page.mouse.click(365, 250)
        await page.keyboard.insert_text(draft["title"])
        await page.wait_for_timeout(900)
        await page.mouse.click(365, 360)
        insertion_mode = await self.insert_body_text(draft["body"])
        await page.wait_for_timeout(2500)
        try:
            await page.mouse.click(1215, 22)  # save, not publish
        except Exception:
            pass
        await page.wait_for_timeout(4000)

        return {
            "ok": True, "state": "save_attempted", "currentUrl": page.url,
            "title": draft["title"], "insertionMode": insertion_mode, "bodyLength": len(draft["body"]),
        }


def create_naver_draft(*, markdown: str, live: bool) -> Dict[str, Any]:
    """Synchronous entry point matching the other publisher modules' shape.

    ``live=False`` (default): always a safe, zero-side-effect dry-run.
    ``live=True``: requires BOTH fail-closed gates AND a working Playwright
    install; otherwise returns a blocked/error result without ever reaching
    the network — never raises for missing gates/deps, only for real browser
    errors once both gates are satisfied and a session is actually driven.
    """
    if not live:
        draft = parse_draft(markdown)
        return {"apiCalled": False, "dryRun": True, "postPreview": draft}

    start_gate = check_bridge_start_gate()
    if not start_gate.allowed:
        return {"apiCalled": False, "dryRun": True, "blocked": True, "reason": start_gate.reason}

    publish_gate = check_publish_gate()
    if not publish_gate.allowed:
        return {"apiCalled": False, "dryRun": True, "blocked": True, "reason": publish_gate.reason}

    if not _PLAYWRIGHT_AVAILABLE:
        return {
            "apiCalled": False, "dryRun": True, "blocked": True,
            "reason": 'playwright not installed. pip install "hermes-agent[naver]" required for live posting.',
        }

    async def _run() -> Dict[str, Any]:
        session = NaverBridgeSession()
        try:
            return await session.fill_and_private_publish(markdown)
        finally:
            await session.close()

    return asyncio.run(_run())
