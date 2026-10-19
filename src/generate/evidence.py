"""근거 선별 - 검색된 조각을 서비스 단위로 묶어 답변에 쓸 근거(제도 3~5개)를 고른다.

구조도의 '근거 선별' 단계. 같은 서비스의 여러 항목 조각을 한 묶음으로 합치고,
정형 조건 값이 비어 있거나 자격이 불명확한 제도는 needs_check 대상으로 표시한다.
"""
from __future__ import annotations

from src.common import config
from src.common.schema import Chunk


def select_evidence(chunks: list[Chunk], max_services: int = config.MAX_RECOMMENDATIONS) -> dict[str, list[Chunk]]:
    """service_id -> 해당 서비스의 근거 조각들."""
    raise NotImplementedError
