"""② 분할 - 구조 기반 분할 + 긴 항목은 Recursive 보조 (기획서 8.1).

- 서비스 1건을 SECTIONS(지원대상/선정기준/지원내용/신청방법) 항목별로 나눈다.
- 각 조각 text 앞에 서비스명을 접두해 어느 제도의 조각인지 보존한다.
- 항목이 MAX_CHUNK_CHARS를 넘으면 문단·줄바꿈·문장 순으로 Recursive 분할한다 (chunk_id에 -n 부여).
- 빈 항목은 조각을 만들지 않는다. 서비스 목적 요약 등 항목 밖 텍스트의 처리는 A가 결정한다.
"""
from __future__ import annotations

from src.common import config
from src.common.schema import SECTIONS, Chunk  # noqa: F401


def chunk_service(service: dict, max_chars: int = config.MAX_CHUNK_CHARS) -> list[Chunk]:
    raise NotImplementedError


def split_long_text(text: str, max_chars: int) -> list[str]:
    raise NotImplementedError
