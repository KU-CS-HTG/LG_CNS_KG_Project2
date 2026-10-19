"""① 로드 - 정제된 서비스를 DB(SQLite)에 적재.

DB는 (1) 정형 필터의 원천, (2) Baseline(SQL+키워드) 실험의 원천, (3) 변경분 재색인 판단의 원천이다.
"""
from __future__ import annotations

from src.common.schema import Metadata


def clean_service(raw: dict, conditions: dict | None = None) -> dict:
    """원본 1건을 정제해 dict로 반환.

    - 공백·빈값 정리, 표/목록 깨짐 보정
    - 신청기한을 ISO 날짜로 파싱 (상시·미정은 None)
    - supportConditions(정형 지원조건)을 병합해 Metadata(연령·지역 등) 구성
    - content_hash: 본문 필드 해시 (변경 감지용)
    """
    raise NotImplementedError


def is_youth_service(service: dict) -> bool:
    """청년 대상 서비스인지 판정 (데이터 범위, README 1.1).

    제안 기준(팀 합의 필요): 지원조건의 연령 범위가 config.YOUTH_AGE_RANGE와 겹치거나,
    서비스명·지원대상·선정기준에 config.YOUTH_KEYWORDS가 들어 있으면 청년 대상으로 본다.
    연령 정보가 없고 키워드도 없으면 제외한다. 범위에서 빠진 서비스는 DB·색인에 넣지 않는다.
    """
    raise NotImplementedError


def build_metadata(service: dict) -> Metadata:
    raise NotImplementedError


def upsert_services(services: list[dict]) -> list[str]:
    """service_id 기준 upsert. 신규·변경(content_hash 달라짐)된 service_id 목록을 반환 -> 변경분만 재색인."""
    raise NotImplementedError


def mark_closed() -> list[str]:
    """API에서 사라진 서비스를 종료 처리하고 service_id 목록 반환 (색인에서 제거)."""
    raise NotImplementedError


def load_services(service_ids: list[str] | None = None) -> list[dict]:
    raise NotImplementedError


def sql_keyword_search(keywords: list[str], k: int) -> list[dict]:
    """Baseline용 SQL LIKE 키워드 검색은 retrieve/baseline.py가 호출한다 (DB 접근은 이 모듈에서)."""
    raise NotImplementedError
