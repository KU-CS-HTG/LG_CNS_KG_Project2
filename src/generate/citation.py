"""인용 형식 - 서비스명, 소관기관, 근거 문구, 상세 링크."""
from __future__ import annotations

from src.common.schema import Chunk, Citation

DISCLAIMER = "최종 신청 자격은 담당 기관에서 확인하세요."


def build_citations(chunks: list[Chunk]) -> list[Citation]:
    raise NotImplementedError
