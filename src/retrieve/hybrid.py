"""⑤ 검색 - Hybrid(Vector + BM25), Top-K, 중복 제거. B -> C 인터페이스."""
from __future__ import annotations

from src.common import config
from src.common.schema import Chunk


def search(query: str, filters: dict, k: int = config.DEFAULT_TOP_K) -> list[Chunk]:
    raise NotImplementedError
