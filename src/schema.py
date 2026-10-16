"""팀 공통 데이터 형식 — 세 모듈(수집·검색·생성)이 이 형식으로 주고받는다.

data/processed/services.jsonl 의 한 줄 = Service 하나.
"""
from typing import TypedDict


class Service(TypedDict):
    service_id: str
    name: str            # 서비스명
    summary: str         # 서비스목적요약 (한 줄 요약)
    agency: str          # 소관기관
    region: str          # "전국" 또는 "서울특별시" 등
    min_age: int         # 조건 없으면 0
    max_age: int         # 조건 없으면 200
    category: str        # 서비스분야 (예: 주거·자립)
    support_type: str    # 지원유형 (예: 현금)
    target: str          # 지원대상 (텍스트)
    criteria: str        # 선정기준 (텍스트)
    content: str         # 지원내용 (텍스트)
    how_to_apply: str    # 신청방법
    deadline: str        # 신청기한
    reception: str       # 접수기관
    contact: str         # 전화문의
    url: str             # 상세 페이지
    user_type: str       # "개인" / "가구" / (확장 시) "소상공인", "법인"
    source: str          # "gov24" / (확장 시) "bizinfo"


class UserProfile(TypedDict):
    age: int
    region: str          # 예: "서울특별시"


def to_document_text(s: Service) -> str:
    """임베딩·LLM 근거로 쓸 본문: 비정형 텍스트 필드를 합친다.

    샘플 데이터처럼 일부 필드가 없을 수 있어 .get()으로 꺼내고, 빈 항목은 뺀다.
    """
    sections = [
        ("서비스명", s.get("name")),
        ("요약", s.get("summary")),
        ("지원대상", s.get("target")),
        ("선정기준", s.get("criteria")),
        ("지원내용", s.get("content")),
        ("신청방법", s.get("how_to_apply")),
        ("신청기한", s.get("deadline")),
        ("접수기관", s.get("reception")),
        ("문의", s.get("contact")),
    ]
    return "\n".join(f"[{label}] {value}" for label, value in sections if value)


def to_metadata(s: Service) -> dict:
    """필터링·화면 표시에 쓸 정형 필드 (Chroma 메타데이터는 str/int/float/bool만 가능)."""
    return {
        "service_id": s["service_id"],
        "name": s["name"],
        "agency": s["agency"],
        "region": s["region"],
        "min_age": int(s["min_age"]),
        "max_age": int(s["max_age"]),
        "category": s.get("category", ""),
        "support_type": s.get("support_type", ""),
        "url": s["url"],
        # 샘플 데이터처럼 필드가 없으면 1단계 기본값 사용
        "user_type": s.get("user_type", "개인"),
        "source": s.get("source", "gov24"),
    }