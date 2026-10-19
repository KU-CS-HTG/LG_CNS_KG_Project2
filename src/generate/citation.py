"""인용 형식 - 서비스명, 소관기관, 근거 문구, 상세 링크 (Citation Accuracy 평가 대상)."""
from __future__ import annotations

from src.common.schema import Chunk, Citation

DISCLAIMER = "최종 신청 자격은 담당 기관에서 확인하세요."
NO_EVIDENCE_MESSAGE = "수집된 자료에서 근거를 찾지 못했습니다. 담당 기관 또는 정부24 등 공식 사이트에서 확인해 주세요."


def build_citations(chunks: list[Chunk]) -> list[Citation]:
    raise NotImplementedError
