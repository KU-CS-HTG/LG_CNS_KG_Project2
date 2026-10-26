"""W1: 사용자가 이미 이용 중인 제도를 추천에서 빼기. 기반 단계에서는 아무것도 바꾸지 않는 자리표시자다 (docs/WORK_ITEMS.md W1).

`generator._answer_with_evidence`가 검색·지역 거르기 뒤, 근거 선별 앞에서 호출한다. 구현 담당은 이 파일과 condition_extractor만 고친다."""
from __future__ import annotations

from src.common.schema import Chunk


def exclude_in_use(question: str, conditions: dict, chunks: list[Chunk]) -> list[Chunk]:
    """이미 이용 중인 제도의 조각을 뺀 목록을 돌려준다. 지금은 그대로 돌려준다."""
    return chunks
