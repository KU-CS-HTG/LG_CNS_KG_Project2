"""W4: 제도 카드의 신청기간 '자세히 확인'. 자리표시자: 아무것도 그리지 않는다 (docs/WORK_ITEMS.md W4).

`app/main.py`의 `_card`가 카드 머리 바로 아래에서 호출한다. 구현 담당은 이 파일만 고친다 (main.py를 고치지 않는다).
r.<필드>가 비어 있으면 아무것도 그리지 않아야 한다."""
from __future__ import annotations

from src.common.schema import Recommendation


def render(r: Recommendation, key: str) -> None:
    """key는 카드마다 다른 문자열이다. Streamlit 위젯 key를 만들 때 접두로 쓴다 (예: f"card_period_{key}")."""
    return None
