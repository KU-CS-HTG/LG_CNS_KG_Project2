"""W3: 실제 신청 사이트 링크. 자리표시자: 빈 문자열(링크 없음)을 돌려준다 (docs/WORK_ITEMS.md W3).

`generator._to_recommendation`이 `Recommendation.apply_url`을 채울 때 호출한다. 찾지 못하면 빈 문자열이어야 한다(없는 링크를 만들지 않는다)."""
from __future__ import annotations

from src.common.schema import Chunk


def find(chunks: list[Chunk], how_to_apply: str) -> str:
    return ""
