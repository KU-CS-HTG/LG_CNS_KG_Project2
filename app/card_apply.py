"""W3: 제도 카드의 '어떻게 신청하나요' 실제 신청 링크 (docs/WORK_ITEMS.md W3).

`app/main.py`의 `_card`가 카드 머리(무엇을 받나요·어떻게 신청하나요 상자) 바로 아래에서 호출한다.
링크가 있으면 '신청하기' 버튼(주소 이름과 함께), 없으면 '신청 사이트를 찾지 못함 — 담당 기관 확인'을 보인다
(없는 링크를 만들지 않는다). 정부24 상세는 카드 맨 아래 '상세 페이지 열기'로 남는다(보조 링크).
CSS는 main.py의 카드 스타일(.links, a.btn)을 그대로 쓴다.
"""
from __future__ import annotations

import html
import re

import streamlit as st

from src.common.schema import Recommendation
from src.generate import apply_link

NOT_FOUND_MESSAGE = "신청 사이트를 찾지 못함 — 담당 기관 확인"
# 링크 종류별 버튼 문구 (apply_link.kind). 첫 화면으로 가는 링크를 '바로가기'처럼 보이게 하지 않는다
BUTTON = {"page": "📝 이 제도 신청 페이지 바로가기 ↗", "search": "🔎 고용24에서 이 제도 찾기 ↗",
          "gov24": "📝 정부24에서 이 제도 신청하기 ↗", "email": "✉️ 이메일로 신청하기",
          "link": "🔗 기관이 안내한 신청 페이지 열기 ↗", "home": "🌐 신청 사이트 열기 ↗"}
NOTE = {"page": "", "search": "검색 결과에서 신청 메뉴를 고르세요", "gov24": "", "email": "신청방법의 제출 서류를 첨부해 보내세요", "link": "",
        "home": "사이트에서 제도 이름으로 찾아 주세요"}


def render(r: Recommendation, key: str) -> None:
    """key는 카드마다 다른 문자열이다. Streamlit 위젯 key를 만들 때 접두로 쓴다 (예: f"card_apply_{key}")."""
    st.markdown(apply_html(r), unsafe_allow_html=True)


def apply_html(r: Recommendation) -> str:
    """신청하기 버튼 줄 (테스트에서 Streamlit 없이 확인)."""
    url = r.apply_url if r.apply_url.startswith(("https://", "http://", "mailto:")) else ""
    if not url:
        agency = f"({html.escape(r.agency)})" if r.agency else ""
        return (f'<div style="font-size:.8rem;opacity:.7;margin:-.2rem 0 .6rem">📝 {NOT_FOUND_MESSAGE}{agency}</div>')
    kind = apply_link.kind(url) or "home"
    host = re.sub(r"^(?:https?://|mailto:)", "", url).split("/")[0]   # 이메일 접수면 주소를 그대로 보인다 (복사용)
    note = " · ".join(x for x in (host, NOTE[kind]) if x)
    step = apply_link.next_step(url)
    pre = apply_link.before(url)
    pre_btn = (f'<a class="btn" href="{html.escape(pre[1])}" target="_blank" rel="noopener">① {html.escape(pre[0])} ↗</a>'
               if pre else "")
    step_line = (f'<div style="font-size:.82rem;margin:-.4rem 0 .7rem">👉 페이지에서 <b>{html.escape(step)}</b></div>'
                 if step else "")
    return (f'<div class="links" style="justify-content:flex-start;align-items:center;gap:.5rem;margin:-.2rem 0 .7rem">'
            f'{pre_btn}<a class="btn" href="{html.escape(url)}" target="_blank" rel="noopener">{"② " if pre else ""}{BUTTON[kind]}</a>'
            f'<span style="font-size:.78rem;opacity:.65">{html.escape(note)}</span></div>' + step_line)
