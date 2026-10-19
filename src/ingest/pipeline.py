"""A 파이프라인 진입점: 수집 -> 정제/적재 -> 분할.  실행: python -m src.ingest.pipeline"""
from __future__ import annotations

from src.common.schema import Chunk


def run() -> list[Chunk]:
    raise NotImplementedError


if __name__ == "__main__":
    run()
