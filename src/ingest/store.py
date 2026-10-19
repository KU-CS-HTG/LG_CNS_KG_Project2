"""① 로드 - 정제된 서비스를 DB(SQLite)에 적재. 정형 필터·Baseline(SQL+키워드)의 원천."""
from __future__ import annotations


def clean_service(raw: dict, conditions: dict | None = None) -> dict:
    """원본 1건을 정제(공백/빈값 정리, 신청기한 파싱, 지원조건 병합)해 dict로 반환."""
    raise NotImplementedError


def save_services(services: list[dict]) -> None:
    """services 테이블에 upsert (service_id 기준)."""
    raise NotImplementedError


def load_services() -> list[dict]:
    raise NotImplementedError
