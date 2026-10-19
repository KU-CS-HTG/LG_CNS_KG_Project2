"""④ 벡터 저장소 - 조각 + 메타데이터 저장, 벡터/BM25 조회. 오프라인 색인은 문서가 바뀔 때만 실행."""
from __future__ import annotations

from src.common.schema import Chunk


def build_index(chunks: list[Chunk]) -> None:
    """조각을 임베딩해 저장. chunk_id 기준 upsert라 변경분만 넣어도 된다."""
    raise NotImplementedError


def remove_services(service_ids: list[str]) -> None:
    """종료·삭제된 서비스의 조각을 제거."""
    raise NotImplementedError


def vector_search(query: str, k: int, where: dict | None = None) -> list[tuple[Chunk, float]]:
    raise NotImplementedError


def bm25_search(query: str, k: int, where: dict | None = None) -> list[tuple[Chunk, float]]:
    """제도명·소관기관·지역명 같은 정확한 일치를 위한 키워드 검색."""
    raise NotImplementedError
