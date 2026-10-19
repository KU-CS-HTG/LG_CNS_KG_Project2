"""정형 조건 필터. 소프트 적용: 값이 비어 있으면 통과(확인 필요), 신청기한이 지난 제도는 제외."""
from __future__ import annotations

from src.common.schema import Chunk


def apply_filters(chunks: list[Chunk], filters: dict) -> list[Chunk]:
    raise NotImplementedError


def relax(filters: dict) -> dict:
    """결과가 없을 때 필터를 한 단계 풀어 반환."""
    raise NotImplementedError
