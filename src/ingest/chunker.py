"""② 분할 - 서비스 항목별(지원대상/선정기준/지원내용/신청방법) 구조 기반 분할.

각 조각 text 앞에 서비스명을 접두한다. 반환 형식은 src.common.schema.Chunk.
"""
from __future__ import annotations

from src.common.schema import Chunk

SECTIONS = ("지원대상", "선정기준", "지원내용", "신청방법")


def chunk_service(service: dict) -> list[Chunk]:
    raise NotImplementedError
