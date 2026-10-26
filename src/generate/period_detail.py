"""W4: 신청기한이 설명형('과정별 모집기간 상이' 등)일 때 보여 줄 원문 전체. 자리표시자: 빈 문자열 (docs/WORK_ITEMS.md W4).

`generator._to_recommendation`이 `Recommendation.period_detail`을 채울 때 호출한다. 일정이 원문에 없으면 빈 문자열이다."""
from __future__ import annotations


def explain(period: str) -> str:
    return ""
