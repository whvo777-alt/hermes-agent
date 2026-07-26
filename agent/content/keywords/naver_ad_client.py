"""Low-level Naver Search Ad API client (HMAC-signed requests).

Only wraps the one endpoint keyword_expander needs -- keywordstool, the
"연관키워드 조회" (related keywords) call. Credentials come from
NAVER_AD_API_KEY / NAVER_AD_SECRET_KEY / NAVER_AD_CUSTOMER_ID in the
environment (already loaded by the normal Hermes .env flow). Validated
against the real API in this session (2026-07-23) with keyword "자기계발".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Dict, List

import requests

_BASE_URL = "https://api.naver.com"
_KEYWORDSTOOL_URI = "/keywordstool"
_MAX_SEEDS_PER_CALL = 5


class NaverAdApiError(RuntimeError):
    """Raised on missing credentials or a non-200 API response."""


def _signature(timestamp: str, method: str, uri: str, secret_key: str) -> str:
    message = f"{timestamp}.{method}.{uri}"
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class NaverAdClient:
    def __init__(self, *, api_key: str = "", secret_key: str = "", customer_id: str = ""):
        self.api_key = api_key or os.environ.get("NAVER_AD_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("NAVER_AD_SECRET_KEY", "")
        self.customer_id = customer_id or os.environ.get("NAVER_AD_CUSTOMER_ID", "")
        if not (self.api_key and self.secret_key and self.customer_id):
            raise NaverAdApiError(
                "NAVER_AD_API_KEY / NAVER_AD_SECRET_KEY / NAVER_AD_CUSTOMER_ID missing"
            )

    def _headers(self, method: str, uri: str) -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        return {
            "X-Timestamp": timestamp,
            "X-API-KEY": self.api_key,
            "X-Customer": self.customer_id,
            "X-Signature": _signature(timestamp, method, uri, self.secret_key),
        }

    def related_keywords(self, seeds: List[str]) -> List[Dict]:
        """Naver's "연관키워드 조회" -- up to 5 seeds per call, comma-joined."""
        if not seeds:
            return []
        if len(seeds) > _MAX_SEEDS_PER_CALL:
            raise NaverAdApiError(
                f"최대 {_MAX_SEEDS_PER_CALL}개 시드만 한 번에 조회 가능 (받은 개수: {len(seeds)})"
            )

        headers = self._headers("GET", _KEYWORDSTOOL_URI)
        params = {"hintKeywords": ",".join(seeds), "showDetail": "1"}
        try:
            resp = requests.get(_BASE_URL + _KEYWORDSTOOL_URI, headers=headers, params=params, timeout=15)
        except requests.RequestException as exc:
            raise NaverAdApiError(f"Naver Ad API 요청 실패: {exc}") from exc

        if resp.status_code != 200:
            raise NaverAdApiError(f"Naver Ad API HTTP {resp.status_code}: {resp.text}")

        return resp.json().get("keywordList", [])
