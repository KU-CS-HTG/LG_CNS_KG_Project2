"""W2: 지원내용 상세(조건별 금액 문장). 자리표시자: 빈 목록을 돌려준다 (docs/WORK_ITEMS.md W2).

`generator._to_recommendation`이 `Recommendation.benefit_detail`을 채울 때 호출한다. 원문에 있는 금액·조건만 쓴다."""
from __future__ import annotations

from src.common.schema import Chunk


def extract(chunks: list[Chunk]) -> list[str]:
    return []
