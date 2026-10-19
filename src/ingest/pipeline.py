"""A 파이프라인 진입점 (① 로드 -> ② 분할). 청년 대상 서비스만 남긴다(store.is_youth_service). 실행: python -m src.ingest.pipeline [--full]

정기 배치(예: 1일 1회)로 돌리며 기본은 증분이다: 신규·변경 서비스만 조각을 다시 만들어 B에게 넘긴다.
"""
from __future__ import annotations

from src.common.schema import Chunk


def run(full: bool = False) -> list[Chunk]:
    """1) client로 수집·원본 저장  2) store로 정제 -> 청년 대상 필터 -> upsert(변경분 파악)  3) chunker로 분할.
    full=True면 전체 재생성. 반환: 재색인이 필요한 조각 (종료된 서비스 ID는 store.mark_closed로 별도 처리)."""
    raise NotImplementedError


if __name__ == "__main__":
    run()
