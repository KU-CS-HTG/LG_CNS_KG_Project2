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


def load_search_cases() -> list[dict]:
    """B -> C 인계 샘플. 각 case의 results는 sample_chunks에서 찾은 Chunk 리스트(관련도 순)."""
    by_id = {c.chunk_id: c for c in load_sample_chunks()}
    data = json.loads((_DIR / "sample_search_results.json").read_text(encoding="utf-8"))
    cases = []
    for c in data["cases"]:
        case = dict(c)
        case["results"] = [by_id[cid] for cid in c["result_chunk_ids"]]
        cases.append(case)
    return cases


def service_ids(chunks: list[Chunk]) -> list[str]:
    """순서를 유지한 service_id 목록 (조각 -> 서비스 단위 순위로 변환)."""
    seen: list[str] = []
    for c in chunks:
        if c.service_id not in seen:
            seen.append(c.service_id)
    return seen
