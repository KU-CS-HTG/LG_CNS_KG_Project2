"""④ 벡터 저장소 - 조각 + 메타데이터 저장 및 벡터/BM25 조회."""
from __future__ import annotations

from src.common.schema import Chunk


def build_index(chunks: list[Chunk]) -> None:
    """오프라인 색인. 문서가 바뀔 때만 실행."""
    raise NotImplementedError


def vector_search(query: str, k: int, where: dict | None = None) -> list[tuple[Chunk, float]]:
    raise NotImplementedError


def bm25_search(query: str, k: int, where: dict | None = None) -> list[tuple[Chunk, float]]:
    raise NotImplementedError
