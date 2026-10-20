"""근거 선별 - 검색된 조각을 서비스 단위로 묶어 답변에 쓸 근거(제도 3~5개)를 고른다.

구조도의 '근거 선별' 단계. 같은 서비스의 여러 항목 조각을 한 묶음으로 합치고,
정형 조건 값이 비어 있거나 자격이 불명확한 제도는 needs_check 대상으로 표시한다.
"""
from __future__ import annotations

import logging
import re

from src.common import config
from src.common.schema import SECTIONS, Chunk
from src.generate.condition_extractor import REGIONS
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


_AGE_TEXT_RE = re.compile(r"\d+\s*(세|살)|\d{2,4}\s*년\s*생")
_REGION_TEXT_RE = re.compile("|".join(("주민등록", "거주", *REGIONS)))


def _missing_conditions(chunks: list[Chunk]) -> list[str]:
    """정형 조건 값이 비어 있어 단정할 수 없는 항목 ("확인 필요"로 표시할 목록, 사용자 문구).

    메타데이터가 비어 있어도 근거 원문에 지역·나이가 적혀 있으면 넣지 않는다 (원문 조건은 LLM이 비교해
    이유·확인 필요에 반영한다). 실제 데이터는 지역·나이 메타데이터가 아직 없어(A 문서 INGEST_A.md)
    모든 카드에 같은 줄이 붙고, 원문에 '서울 19~39세'가 있는데도 '지역 정보 없음'이 나왔다.
    신청기한은 카드의 신청방법 줄에 항상 표시되므로 여기에 넣지 않는다."""
    meta: dict = {}
    for c in chunks:                       # 조각마다 메타데이터가 다를 수 있으므로 값이 있는 쪽을 모은다
        for key, value in c.metadata.items():
            if value not in (None, "", []):
                meta.setdefault(key, value)
    text = " ".join(c.text for c in chunks)

    needs: list[str] = []
    if not meta.get("region") and not _REGION_TEXT_RE.search(text):
        needs.append("거주 지역 조건 (자료에 지역 정보 없음)")
    if meta.get("age_min") is None and meta.get("age_max") is None and not _AGE_TEXT_RE.search(text):
        needs.append("연령 조건 (자료에 연령 정보 없음)")
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
