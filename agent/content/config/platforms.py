"""Platform config — ported from multi-content-pipeline/config/platforms.js.

``capability`` documents each platform's actual publish-client state, ported
unchanged from Repository 2's Phase 16E notes:
- 'draft_only'            real API/client exists and can create a draft;
                           nothing here can move it to published.
- 'not_implemented'       no real publish client exists at all.
- 'disabled_until_governed'
                           a real, publish-capable automation exists but is
                           fail-closed by default, requiring explicit
                           operator opt-in env vars.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Platform:
    id: str
    label: str
    publisher: str
    capability: str  # 'draft_only' | 'not_implemented' | 'disabled_until_governed'
    supports_auto_publish: bool = False


PLATFORMS: List[Platform] = [
    Platform(id="naver", label="네이버 블로그", publisher="naver", capability="disabled_until_governed"),
    Platform(id="tistory", label="티스토리", publisher="tistory", capability="not_implemented"),
    Platform(id="blogspot", label="블로그스팟", publisher="blogspot", capability="draft_only"),
    Platform(id="wordpress", label="WordPress", publisher="wordpress", capability="draft_only", supports_auto_publish=True),
]

_BY_ID = {platform.id: platform for platform in PLATFORMS}


def find_platform(platform_id: str) -> Optional[Platform]:
    return _BY_ID.get(platform_id)
