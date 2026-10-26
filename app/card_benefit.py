"""W2: display supplied support-content blocks; no retrieval or new citations.

The parent card already renders benefit and citations. Empty details leave that
screen intact. Plain text preserves source layout without interpreting HTML or
Markdown, and card-scoped containers keep multiple expanders separate.
"""
from __future__ import annotations

import streamlit as st

from src.common.schema import Recommendation


def render(r: Recommendation, key: str) -> None:
    """Add a collapsed, stateless detail area without repeating the summary."""
    details = [block for block in r.benefit_detail if block.strip()]
    if not details:
        return None
    with st.container(key=f"card_benefit_{key}"):
        with st.expander("무엇을 받나요 — 자세히 보기", expanded=False):
            st.caption("지원내용 원문 기준입니다. 개인 조건에 따라 지급액·지급 여부가 달라질 수 있으니 근거 원문을 함께 확인해 주세요.")
            for block in details:
                st.text(block)
    return None
