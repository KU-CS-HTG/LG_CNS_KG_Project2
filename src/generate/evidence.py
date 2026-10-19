"""근거 선별 - 검색된 조각을 서비스 단위로 묶어 답변에 쓸 근거(제도 3~5개)를 고른다.

구조도의 '근거 선별' 단계. 같은 서비스의 여러 항목 조각을 한 묶음으로 합치고,
정형 조건 값이 비어 있거나 자격이 불명확한 제도는 needs_check 대상으로 표시한다.
"""
from __future__ import annotations

import logging

from src.common import config
from src.common.schema import SECTIONS, Chunk
from src.index import vector_store   # 모듈 속성으로 호출한다 (테스트에서 가짜로 바꿔 끼우기 위함)

log = logging.getLogger(__name__)
_SECTION_ORDER = {s: i for i, s in enumerate(SECTIONS)}


def select_evidence(chunks: list[Chunk], max_services: int = config.MAX_RECOMMENDATIONS) -> dict[str, list[Chunk]]:
    """service_id -> 해당 서비스의 근거 조각들.

    검색 순위(처음 등장한 순서)대로 최대 max_services개 서비스를 고르고,
    서비스 안의 조각은 중복을 빼고 SECTIONS 순서(지원대상 -> 선정기준 -> 지원내용 -> 신청방법)로 정렬한다.
    """
    hits: dict[str, list[Chunk]] = {}
    for c in chunks:
        if c.service_id not in hits:
            if len(hits) >= max_services:
                continue
            hits[c.service_id] = []
        hits[c.service_id].append(c)

    return {sid: _sort_sections(_dedupe(_expand_service(sid, group))) for sid, group in hits.items()}


def _expand_service(service_id: str, hits: list[Chunk]) -> list[Chunk]:
    """같은 서비스의 나머지 항목 조각을 더한다 (B의 vector_store.get_service_chunks, CLAUDE.md 인터페이스 B -> C 제안).

    B는 서비스당 조각 1개만 넘기는데, 조각 1개로 답하면 '지원내용: 자료에 없음'처럼 답이 빈약해진다
    (MinKyu-v1 실험). 그래서 지원대상·선정기준·지원내용·신청방법 전체를 가져온다.
    색인이 비었거나 저장소를 열 수 없으면 검색으로 받은 조각만 쓴다 (서비스가 멈추지 않게).
    """
    try:
        more = vector_store.get_service_chunks(service_id)
    except Exception as e:  # 저장소 없음·라이브러리 없음 등
        log.warning("서비스 전체 항목 조회 실패(%s), 검색 조각만 사용: %s", service_id, e)
        return hits
    return hits + [c for c in more if c.service_id == service_id]


def _missing_conditions(chunks: list[Chunk]) -> list[str]:
    """정형 조건 값이 비어 있어 단정할 수 없는 항목 ("확인 필요"로 표시할 목록, 사용자 문구)."""
    meta: dict = {}
    for c in chunks:                       # 조각마다 메타데이터가 다를 수 있으므로 값이 있는 쪽을 모은다
        for key, value in c.metadata.items():
            if value not in (None, "", []):
                meta.setdefault(key, value)

    needs: list[str] = []
    if not meta.get("region"):
        needs.append("거주 지역 조건 (자료에 지역 정보 없음)")
    if meta.get("age_min") is None and meta.get("age_max") is None:
        needs.append("연령 조건 (자료에 연령 정보 없음)")
    if not meta.get("deadline"):
        needs.append("신청기한 (상시 접수 또는 자료에 정보 없음)")
    return needs


def _dedupe(chunks: list[Chunk]) -> list[Chunk]:
    seen: set[str] = set()
    out = []
    for c in chunks:
        if c.chunk_id not in seen:
            seen.add(c.chunk_id)
            out.append(c)
    return out


def _sort_sections(chunks: list[Chunk]) -> list[Chunk]:
    return sorted(chunks, key=lambda c: _SECTION_ORDER.get(c.section, len(SECTIONS)))
