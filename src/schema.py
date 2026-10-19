"""팀 공통 데이터 형식 — 수집·검색·생성 모듈이 이 형식으로 주고받는다."""
from dataclasses import dataclass, field
from typing import TypedDict


class Service(TypedDict):
    """data/processed/services.jsonl 의 한 줄 = SQLite services 테이블의 한 행."""
    service_id: str
    name: str            # 서비스명
    summary: str         # 서비스목적요약
    agency: str          # 소관기관
    region: str          # "전국" 또는 "서울특별시" 등 (src/regions.py 기준 이름)
    min_age: int         # 조건 없으면 0
    max_age: int         # 조건 없으면 200
    category: str        # 서비스분야
    support_type: str    # 지원유형
    target: str          # 지원대상
    criteria: str        # 선정기준
    content: str         # 지원내용
    how_to_apply: str    # 신청방법
    deadline: str        # 신청기한 (자유 텍스트)
    reception: str       # 접수기관
    contact: str         # 전화문의
    url: str             # 상세 페이지
    user_type: str       # "개인" / "가구" / (확장 시) "소상공인", "법인"
    source: str          # "gov24" / (확장 시) "bizinfo"


# 구조 기반 분할: 이 항목들이 각각 하나의 청크(길면 여러 개)가 된다.
CHUNK_FIELDS = {
    "summary": "요약",
    "target": "지원대상",
    "criteria": "선정기준",
    "content": "지원내용",
    "how_to_apply": "신청방법",
}


@dataclass
class Conditions:
    """사용자 조건. None = 모름 → 그 조건으로는 거르지 않는다(소프트 필터)."""
    age: int | None = None
    region: str | None = None


def age_text(min_age: int, max_age: int) -> str:
    """0~200(조건 없음 기본값)을 사람이 읽는 표현으로."""
    if min_age <= 0 and max_age >= 200:
        return "연령 제한 없음"
    if max_age >= 200:
        return f"{min_age}세 이상"
    if min_age <= 0:
        return f"{max_age}세 이하"
    return f"{min_age}~{max_age}세"


@dataclass
class Evidence:
    field: str           # 예: "선정기준"
    text: str


@dataclass
class ServiceHit:
    """검색 결과 1건 = 서비스 1개 (청크 여러 개가 걸려도 서비스 단위로 합침)."""
    service_id: str
    name: str
    agency: str
    region: str
    min_age: int
    max_age: int
    deadline: str
    how_to_apply: str
    url: str
    score: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)
