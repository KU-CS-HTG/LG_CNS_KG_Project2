"""질문에 없는 특정 대상 전용 제도를 검색 순위에서 뒤로 보낸다 (WORK_ITEMS R1 2단계, 거르지는 않는다).

'대학을 졸업했는데 아직 취업을 못 했어요' 질문에 검색 1위가 장애학생 취업지원, 3위가 장애인 취업성공패키지였다
(dev_v2, 상위 5개 나쁨 판정 사유 not_eligible). 답변 단계는 이미 이런 제도를 빼지만(generator._other_group) 검색이
앞자리에 올려 판정 후보 10자리를 차지했고, 그만큼 정답이 판정받을 기회가 줄었다.
판단 규칙은 generator._other_group을 그대로 쓴다 (규칙이 두 군데로 갈라지지 않게). 질문에 그 대상 말이 있으면
('장애가 있는데', '보훈대상자인데') 감점하지 않는다. 지역 감점(hybrid.penalize_local)과 같은 방식: 점수에서 빼고 다시 정렬.
"""
from __future__ import annotations

from src.common.schema import Chunk

GROUP_PENALTY = 1.0           # RRF 점수(0.03 안팎)에서 빼는 값: 대상 전용 제도를 다른 제도 뒤로 보낸다 (0이면 끔)


def penalize_other_group(ranked: list[tuple[Chunk, float]], query: str,
                         penalty: float = GROUP_PENALTY) -> list[tuple[Chunk, float]]:
    """질문(query)에 없는 대상 전용 제도의 점수에서 penalty를 빼고 다시 정렬한다 (안정 정렬). 거르지는 않는다."""
    if penalty <= 0 or not ranked:
        return ranked
    # 함수 안에서 불러온다 (generator가 hybrid를 불러와서 모듈 맨 위에서 부르면 순환 import, filters.is_local과 같다)
    from src.generate import generator
    cache: dict[str, bool] = {}

    def other(c: Chunk) -> bool:
        if c.service_id not in cache:
            cache[c.service_id] = generator._other_group(query, c.service_name)
        return cache[c.service_id]

    adjusted = [(c, s - penalty if other(c) else s) for c, s in ranked]
    return sorted(adjusted, key=lambda cs: -cs[1])
