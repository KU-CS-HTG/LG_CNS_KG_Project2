"""정형 조건 필터 (기획서 8.1 메타데이터). 하드가 아니라 소프트로 적용한다.

- 값이 비어 있는 메타데이터는 제외하지 않고 후보에 남긴다 ('확인 필요'로 표시되도록 C에 전달).
- 신청기한이 지난 제도는 제외한다.
- 결과가 없으면 relax()로 필터를 단계적으로 풀어 다시 검색한다.
"""
from __future__ import annotations

from src.common.schema import Chunk, Filters


def apply_filters(chunks: list[Chunk], filters: Filters) -> list[Chunk]:
    raise NotImplementedError


def is_expired(chunk: Chunk, today: str | None = None) -> bool:
    raise NotImplementedError


def relax(filters: Filters) -> Filters:
    """한 단계 완화 (예: 관심 분야 -> 지역 -> 연령 순). 더 풀 것이 없으면 빈 필터."""
    raise NotImplementedError
