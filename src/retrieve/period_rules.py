"""신청기한 원문으로 마감이 지난 제도를 검색에서 뺀다 (WORK_ITEMS R1 2단계, hybrid_filter에서만).

B의 기한 필터는 A가 날짜로 해석한 deadline만 본다. '2026년 상반기'·'(1차) 5월 22일~6월 22일'처럼 날짜로 해석되지 않은
기한은 deadline이 비어 지난 제도가 검색 상위에 남았다 (dev_v2 상위 5개 나쁨 사유 expired 4건: '혼자 살 집 보증금' 질문
1위 청년월세 지원 등). 답변 단계는 이미 신청기한 원문으로 거른다(generator._drop_expired) — 같은 규칙을 그대로 쓴다.
- 마감이 확실히 지남 -> 뺀다 (CLAUDE.md: 신청기한이 지난 제도는 후보에서 제외)
- 이번 회차만 끝나고 학기·해마다 다시 모집(국가장학금 등, generator._recurring_closed) -> 빼지 않고 뒤로 (카드에서
  '다음 공고 확인'으로 안내하는 제도라 답변 단계와 같게 남긴다)
원문을 읽을 수 없으면(DB 없음) 거르지 않는다.
"""
from __future__ import annotations

from datetime import date

from src.common.schema import Chunk

RECURRING_PENALTY = 1.0       # 이번 회차가 끝난 반복 모집 제도의 RRF 점수에서 빼는 값 (0이면 끔)
_cache: dict[tuple[str, date], str] = {}   # (service_id, 기준일) -> 'expired' / 'recurring' / '' (질문마다 같은 제도를 다시 읽지 않게)


def _state(service_id: str, today: date) -> str:
    key = (service_id, today)
    if key not in _cache:
        # 함수 안에서 불러온다 (generator가 hybrid를 불러와서 모듈 맨 위에서 부르면 순환 import)
        from src.generate import generator
        period = generator._application_period(service_id)
        if generator._recurring_closed(period, today):
            _cache[key] = "recurring"
        elif generator._period_expired(period, today):
            _cache[key] = "expired"
        else:
            _cache[key] = ""
    return _cache[key]


def apply(ranked: list[tuple[Chunk, float]], today: str | None = None,
          penalty: float | None = None) -> list[tuple[Chunk, float]]:
    """마감이 지난 제도를 빼고, 이번 회차가 끝난 반복 모집 제도는 점수에서 penalty(기본 RECURRING_PENALTY)를 빼 다시 정렬한다."""
    if not ranked:
        return ranked
    penalty = RECURRING_PENALTY if penalty is None else penalty
    day = date.fromisoformat(today) if today else date.today()
    out = []
    for c, s in ranked:
        state = _state(c.service_id, day)
        if state == "expired":
            continue
        out.append((c, s - penalty if state == "recurring" and penalty > 0 else s))
    return sorted(out, key=lambda cs: -cs[1])
