"""⑤ 검색 - Hybrid(Vector + BM25), Top-K, 중복 제거. B -> C 인터페이스.

mode는 평가 실험용 (Vector 단독 / BM25 단독 / Hybrid / Hybrid+정형 필터 비교). 서비스 기본값은 "hybrid_filter".
Reranking·MMR은 문제가 관찰되면 도입한다 (8.2 개선 방법 표에 기록).
"""
from __future__ import annotations

from typing import Literal

from src.common import config
from src.common.schema import Chunk, Filters

Mode = Literal["vector", "bm25", "hybrid", "hybrid_filter"]


def search(
    query: str,
    filters: Filters,
    k: int = config.DEFAULT_TOP_K,
    mode: Mode = "hybrid_filter",
) -> list[Chunk]:
    """결과가 없으면 filters.relax()로 완화해 재검색한다. 같은 service_id의 조각은 중복 제거."""
    raise NotImplementedError


def dedupe(chunks: list[Chunk]) -> list[Chunk]:
    raise NotImplementedError
