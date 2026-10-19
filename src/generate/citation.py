"""인용 형식 - 서비스명, 소관기관, 근거 문구, 상세 링크 (Citation Accuracy 평가 대상)."""
from __future__ import annotations

from src.common.schema import Chunk, Citation

DISCLAIMER = "최종 신청 자격은 담당 기관에서 확인하세요."
NO_EVIDENCE_MESSAGE = "수집된 자료에서 근거를 찾지 못했습니다. 담당 기관 또는 정부24 등 공식 사이트에서 확인해 주세요."
UNKNOWN_AGENCY = "소관기관 정보 없음"
# 근거 없음 응답에 항상 덧붙이는 범위 안내 (README 1.1 '범위 밖 질문'은 C가 결정: 별도 분기 없이 한 줄 안내).
# 청년 나이 기준이 팀 합의 전이라 나이로 범위 밖을 판정하지 않는다.
SCOPE_NOTICE = "이 서비스는 현재 청년 대상 제도를 중심으로 안내하고 있어요."


def _body_text(chunk: Chunk) -> str:
    """조각 본문에서 서비스명 접두(첫 줄)를 뗀 원문. 근거 문구는 항상 실제 조각의 일부여야 한다."""
    text = chunk.text.strip()
    prefix = chunk.service_name.strip()
    if prefix and text.startswith(prefix):
        text = text[len(prefix):].lstrip()
    return text or chunk.text.strip()


def build_citations(chunks: list[Chunk]) -> list[Citation]:
    """조각마다 인용 1개. 같은 서비스의 같은 문구는 한 번만 넣는다 (입력 순서 유지)."""
    citations: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for c in chunks:
        quote = _body_text(c)
        key = (c.service_id, quote)
        if not quote or key in seen:
            continue
        seen.add(key)
        citations.append(Citation(
            service_name=c.service_name,
            agency=c.metadata.get("agency") or UNKNOWN_AGENCY,
            quote=quote,
            detail_url=c.detail_url,
        ))
    return citations
