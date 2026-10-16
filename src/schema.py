"""팀 공통 데이터 형식 — 세 모듈(수집·검색·생성)이 이 형식으로 주고받는다.

data/processed/services.jsonl 의 한 줄 = Service 하나.
"""
from typing import TypedDict


class Service(TypedDict):
    service_id: str
    name: str            # 서비스명
    agency: str          # 소관기관
    region: str          # "전국" 또는 "서울특별시" 등
    min_age: int         # 조건 없으면 0
    max_age: int         # 조건 없으면 200
    target: str          # 지원대상 (텍스트)
    criteria: str        # 선정기준 (텍스트)
    content: str         # 지원내용 (텍스트)
    how_to_apply: str    # 신청방법 (텍스트)
    url: str             # 상세 페이지


class UserProfile(TypedDict):
    age: int
    region: str          # 예: "서울특별시"


def to_document_text(s: Service) -> str:
    """임베딩할 본문: 비정형 텍스트 필드만 합친다."""
    return (
        f"[서비스명] {s['name']}\n"
        f"[지원대상] {s['target']}\n"
        f"[선정기준] {s['criteria']}\n"
        f"[지원내용] {s['content']}\n"
        f"[신청방법] {s['how_to_apply']}"
    )


def to_metadata(s: Service) -> dict:
    """필터링에 쓸 정형 필드 (Chroma 메타데이터는 str/int/float/bool만 가능)."""
    return {
        "service_id": s["service_id"],
        "name": s["name"],
        "agency": s["agency"],
        "region": s["region"],
        "min_age": int(s["min_age"]),
        "max_age": int(s["max_age"]),
        "url": s["url"],
    }
