"""Baseline 검색 (SQL + 키워드). 실험 비교용이며 서비스 경로에서는 쓰지 않는다 (기획서 6.2, 9.4)."""
from __future__ import annotations

from src.common import config
from src.common.schema import Chunk


def search(query: str, filters: dict, k: int = config.DEFAULT_TOP_K) -> list[Chunk]:
    raise NotImplementedError
