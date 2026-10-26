"""W4: 신청기한이 설명형('각 과정(캠퍼스)별 모집기간 상이')일 때 '신청 기간 자세히 확인' (docs/WORK_ITEMS.md W4).

`app/main.py`의 `_card`가 W3 신청 링크 바로 아래에서 호출한다. `r.period_detail`이 비면 아무것도 그리지 않는다.
펼치면 원문 기한 문구 전체와 '일정은 담당 기관에서 확인' 안내, 기관 링크(신청 사이트, 없으면 정부24 상세)를 보인다.
과정별 일정은 API에 없어서 앱이 만들지 않는다.
"""
from __future__ import annotations

import html

import streamlit as st

from src.common.schema import Recommendation

TITLE = "🗓 신청 기간 자세히 확인"
GUIDE = "과정·기관별 일정은 수집된 정보에 없어요. 정확한 일정은 담당 기관에서 확인해 주세요."


def render(r: Recommendation, key: str) -> None:
    """key는 카드마다 다른 문자열이다. Streamlit 위젯 key를 만들 때 접두로 쓴다 (예: f"card_period_{key}")."""
    if not r.period_detail:
        return
    with st.container(key=f"card_period_{key}"):
        with st.expander(TITLE):
            st.markdown(period_html(r), unsafe_allow_html=True)


def period_html(r: Recommendation) -> str:
    """펼친 내용 (테스트에서 Streamlit 없이 확인). 링크가 하나도 없으면 안내만 보인다."""
    body = "<br>".join(html.escape(line) for line in r.period_detail.splitlines() if line.strip())
    links = []
    if r.apply_url.startswith(("https://", "http://")):
        links.append(("신청 사이트", r.apply_url))
    detail = next((c.detail_url for c in r.citations if c.detail_url.startswith(("https://", "http://"))), "")
    if detail:
        links.append(("정부24 상세", detail))
    buttons = "".join(f'<a class="btn" href="{html.escape(u)}" target="_blank" rel="noopener">{n} ↗</a>' for n, u in links)
    agency = f"<b>{html.escape(r.agency)}</b> · " if r.agency else ""
    return (f'<div class="quote">{body}</div>'
            f'<div style="font-size:.82rem;opacity:.75;line-height:1.5">{agency}{GUIDE}</div>'
            + (f'<div class="links" style="justify-content:flex-start;gap:.4rem">{buttons}</div>' if buttons else ""))
