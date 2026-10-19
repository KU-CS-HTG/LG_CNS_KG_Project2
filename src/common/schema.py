"""모듈 간 인터페이스(데이터 계약). 바꿀 때는 CLAUDE.md와 docs/ARCHITECTURE.md부터 고친다.

A(ingest) -> B(index/retrieve): Chunk
B(retrieve) -> C(generate): list[Chunk]
C -> 사용자: Answer
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Chunk:
    chunk_id: str            # "{service_id}#{section}" 형태
    service_id: str
    service_name: str
    section: str             # 지원대상 | 선정기준 | 지원내용 | 신청방법
    text: str                # 서비스명 접두가 붙은 본문
    metadata: dict = field(default_factory=dict)  # 지역, 소관기관, 분야, 신청기한, 대상조건
    detail_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Citation:
    service_name: str
    agency: str              # 소관기관
    quote: str               # 근거 문구
    detail_url: str


@dataclass
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
