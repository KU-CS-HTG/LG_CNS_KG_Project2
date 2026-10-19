"""테스트용 가짜 데이터 로더. A의 구현 없이도 B가 자기 코드를 검증할 수 있게 한다."""
from __future__ import annotations

import json
from pathlib import Path

from src.common.schema import Chunk

_DIR = Path(__file__).parent


def load_sample_chunks() -> list[Chunk]:
    data = json.loads((_DIR / "sample_chunks.json").read_text(encoding="utf-8"))
    return [Chunk(**c) for c in data["chunks"]]


def load_sample_queries() -> list[dict]:
    return json.loads((_DIR / "sample_queries.json").read_text(encoding="utf-8"))["queries"]


def service_ids(chunks: list[Chunk]) -> list[str]:
    """순서를 유지한 service_id 목록 (조각 -> 서비스 단위 순위로 변환)."""
    seen: list[str] = []
    for c in chunks:
        if c.service_id not in seen:
            seen.append(c.service_id)
    return seen
