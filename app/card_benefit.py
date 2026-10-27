"""W2: display supplied support-content blocks; no retrieval or new citations.

The parent card already renders benefit and citations. Empty details leave that
screen intact. Plain text preserves source layout without interpreting HTML or
Markdown, and card-scoped containers keep multiple expanders separate.

내 조건으로 받을 수 있는 지원: 사용자가 버튼을 누르면 제도의 조건별 지원(`benefit_tiers`)을 정리하고, 자격 체크리스트를
이 칸 안에 그려 체크한 조건에 맞는 지원을 바로 보여 준다 (체크할 때마다 LLM을 부르지 않는다).
"""
from __future__ import annotations

import streamlit as st

from src.common.schema import Recommendation
from src.generate import benefit_tiers


def checklist_inside(key: str) -> bool:
    """이번 화면에서 자격 체크리스트를 이 칸 안에 그렸는가 (그렸으면 카드가 따로 또 그리지 않는다)."""
    return bool(st.session_state.get(f"tiers_inside_{key}"))


def _needs_text(r: Recommendation, numbers: list[int]) -> str:
    return ", ".join(r.needs_check[n - 1].split(" — ")[0] if 0 < n <= len(r.needs_check) else str(n) for n in numbers)


def _tier_line(t: dict) -> None:
    badge = "" if t["basis"] == benefit_tiers.SOURCE else "  ·  _자료 밖 정보(정확하지 않을 수 있어요)_"
    st.markdown(f"**{t['label']}** — {t['benefit']}{badge}")
    if t["quote"]:
        st.caption(f"원문: {t['quote']}")


def _tier_panel(r: Recommendation, key: str, draw_checklist) -> None:
    if not r.service_id:
        return
    st.session_state[f"tiers_inside_{key}"] = False         # 이번 화면에서 다시 정한다 (체크리스트를 그리면 True)
    open_key, cache = f"tiers_open_{key}", st.session_state.setdefault("tiers_cache", {})
    with st.container(key=f"card_tiers_{key}"):
        if not st.session_state.get(open_key):
            if not st.button("🎁 내 조건으로 받을 수 있는 지원 보기", key=f"tiers_btn_{key}"):
                return
            st.session_state[open_key] = True
        ckey = (r.service_id, tuple(r.needs_check))
        if ckey not in cache:
            with st.spinner("조건별 지원을 정리하는 중…"):
                result = benefit_tiers.compute(r.service_id, r.service_name, list(r.needs_check))
            if result is None:
                st.session_state[open_key] = False
                st.warning("조건별 지원을 정리하지 못했어요. 아래 원문과 자격 체크리스트를 확인해 주세요.")
                return
            cache[ckey] = result
        result = cache[ckey]
        st.markdown("**🎁 무엇을 받나요 — 내 조건으로 보기**")
        if r.needs_check and draw_checklist is not None:
            st.caption("해당하는 조건을 체크하면 받을 수 있는 지원이 아래에 나타나요.")
            draw_checklist()
            st.session_state[f"tiers_inside_{key}"] = True
        checks = st.session_state.get("checks", {})
        checked = {j + 1 for j in range(len(r.needs_check)) if checks.get(f"chk_{key}_{j}")}
        groups = benefit_tiers.evaluate(result["tiers"], checked)
        got = groups["base"] + groups["met"]
        st.markdown("##### ✅ 지금 체크한 조건으로 받을 수 있는 지원")
        if got:
            for t in got:
                _tier_line(t)
        else:
            st.info("아직 해당하는 지원이 없어요. 위에서 해당하는 조건을 체크해 보세요.")
        if groups["pending"]:
            with st.expander(f"조건을 더 체크하면 받을 수 있는 지원 {len(groups['pending'])}가지", expanded=not got):
                for t in groups["pending"]:
                    _tier_line(t)
                    st.caption(f"필요한 조건: {_needs_text(r, t['requires'])}  (아직 체크 안 함: {_needs_text(r, t['missing'])})")
        if result["notes"]:
            st.caption("참고 (자료 밖 일반 정보라 정확하지 않을 수 있어요): " + " / ".join(n["text"] for n in result["notes"]))
        st.caption("체크는 스스로 확인한 결과예요. 실제 자격과 지급액은 소득·가구 등에 따라 달라지니 최종 내용은 담당 기관에서 확인해 주세요.")
        if st.button("닫기", key=f"tiers_close_{key}"):
            st.session_state[open_key] = False
            st.session_state[f"tiers_inside_{key}"] = False
            st.rerun()


def render(r: Recommendation, key: str, draw_checklist=None) -> None:
    """Add a collapsed, stateless detail area without repeating the summary.
    draw_checklist: 자격 체크리스트를 그리는 함수 (카드가 넘긴다). 있으면 '내 조건으로 보기' 칸 안에 체크리스트를 그린다."""
    _tier_panel(r, key, draw_checklist)
    details = [block for block in r.benefit_detail if block.strip()]
    if not details:
        return None
    with st.container(key=f"card_benefit_{key}"):
        with st.expander("무엇을 받나요 — 자세히 보기", expanded=False):
            st.caption("지원내용 원문 기준입니다. 개인 조건에 따라 지급액·지급 여부가 달라질 수 있으니 근거 원문을 함께 확인해 주세요.")
            for block in details:
                st.text(block)
    return None
