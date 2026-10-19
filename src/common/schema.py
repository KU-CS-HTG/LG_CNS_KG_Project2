"""모듈 간 인터페이스(데이터 계약). 바꿀 때는 CLAUDE.md와 docs/ARCHITECTURE.md부터 고친다.

A(ingest) -> B(index/retrieve): Chunk
C(generate) -> B(retrieve):     Filters, search(query, filters, k)
B(retrieve) -> C(generate):     list[Chunk]
C -> 사용자(app):                Answer  (사전기획서 5.1 '최종 경험', 6장 답변 규칙)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TypedDict

# 조각 섹션 (사전기획서 8.1 문서 분할)
SECTIONS = ("지원대상", "선정기준", "지원내용", "신청방법")


class Metadata(TypedDict, total=False):
    """조각 메타데이터 (8.1 메타데이터). 값이 비어 있으면 키를 생략하거나 None. 필터는 비어 있으면 통과."""
    region: str | None        # 지역 (전국이면 None 또는 "전국")
    agency: str               # 소관기관
    field: str                # 서비스분야 / 지원유형
    deadline: str | None      # 신청기한 (ISO 날짜. 상시·미정이면 None)
    age_min: int | None       # 대상 조건: 최소 연령
    age_max: int | None       # 대상 조건: 최대 연령
    target_tags: list[str]    # 대상 조건: 청년, 신혼, 구직 등


class Filters(TypedDict, total=False):
    """검색 조건. 키는 사용자 조건 기준이며 값이 없으면 키를 생략한다.

    거르는 조건(필터, 소프트 적용 8.1: 값이 비어 있는 제도는 통과): age, region, today
    거르지 않는 신호(자격이 아니라 관심사): field(맞는 분야의 제도에 가산점), situation(현재 검색에는 쓰이지 않음)
    """
    age: int
    region: str
    field: str                # 관심 분야. 필터가 아니라 가산점 (제도 분야 이름이 달라도 제도가 사라지지 않는다)
    situation: list[str]
    today: str                # 신청기한이 지난 제도 제외 기준일(ISO). 미지정이면 오늘


@dataclass
class Chunk:
    chunk_id: str            # "{service_id}#{section}" 형태 (긴 항목이 나뉘면 "#{section}-{n}")
    service_id: str
    service_name: str
    section: str             # SECTIONS 중 하나
    text: str                # 서비스명 접두가 붙은 본문
    metadata: Metadata = field(default_factory=dict)  # type: ignore[assignment]
    detail_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Citation:
    service_name: str
    agency: str              # 소관기관
    quote: str               # 근거 원문 문구
    detail_url: str


@dataclass
class Recommendation:
    """답변에 담기는 제도 1건 (질문 1번에 3~5건)."""
    service_name: str
    agency: str
    reason: str              # 해당 가능 이유
    benefit: str             # 지원내용
    how_to_apply: str        # 신청방법·기한
    needs_check: list[str] = field(default_factory=list)  # 확인이 필요한 조건 ("확인 필요" 표시)
    citations: list[Citation] = field(default_factory=list)


@dataclass
class Answer:
    text: str                # 사용자에게 보이는 한국어 답변 전문
    recommendations: list[Recommendation] = field(default_factory=list)
    no_evidence: bool = False  # 근거가 없어 '모른다'고 답한 경우 (담당 기관·공식 사이트 안내)
