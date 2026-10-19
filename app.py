"""[C. UI] Streamlit 데모

실행:
    streamlit run app.py
"""
import streamlit as st

from src import conditions
from src.config import DEFAULT_MODE, DEFAULT_TOP_K, SEARCH_MODES
from src.generator import describe, generate_stream, place_text
from src.regions import SIDO
from src.retriever import search_multi
from src.schema import age_text

AUTO = "(질문에서 자동 추출)"

st.set_page_config(page_title="정부 지원제도 맞춤 안내", page_icon="🧾")
st.title("🧾 나에게 맞는 정부 지원제도")
st.caption("생활 상황을 평소 말투로 적어 주세요. 예: 서울 사는 27살 취준생인데 월세가 부담돼요")

with st.sidebar:
    st.header("내 정보 (선택)")
    st.caption("입력하면 질문에서 뽑은 값보다 우선합니다.")
    use_age = st.checkbox("나이 직접 입력")
    age = st.number_input("나이", min_value=0, max_value=120, value=25, disabled=not use_age)
    region = st.selectbox("거주지 (시·도)", [AUTO, *SIDO])
    sigungu = st.text_input("시·군·구 (선택)", placeholder="예: 노원구, 수원시")

    st.header("검색 설정")
    mode = st.selectbox("검색 방식", SEARCH_MODES, index=SEARCH_MODES.index(DEFAULT_MODE))
    k = st.slider("최대 제도 수", 3, 10, DEFAULT_TOP_K)

question = st.text_area("궁금한 내용", height=80)

if st.button("찾아보기", type="primary") and question.strip():
    try:
        with st.spinner("질문 분석 중..."):
            plan = conditions.analyze(question)
            cond = conditions.merge(
                plan.conditions,
                override_age=int(age) if use_age else None,
                override_region=None if region == AUTO else region,
                override_sigungu=sigungu.strip() or None,
            )
        st.info(f"적용 조건: {describe(cond)}  ·  분야: {plan.category or '없음'}  ·  검색 방식: {mode}\n\n"
                f"검색어: {plan.search_query}")
        with st.spinner("검색 중..."):
            # 원문 + 재작성 검색어를 둘 다 검색해 합침 (한쪽이 빗나가도 다른 쪽이 받쳐 줌)
            hits = search_multi([question, plan.search_query], cond, mode=mode, k=k, category=plan.category)
        st.divider()
        st.write_stream(generate_stream(question, cond, hits))   # 생성되는 대로 바로 표시
    except Exception as e:  # 키 누락, DB·벡터DB 미생성 등을 화면에 표시
        st.error(f"오류: {e}")
    else:
        with st.expander(f"검색된 근거 문서 {len(hits)}건"):
            for i, h in enumerate(hits, 1):
                st.markdown(f"**[{i}] {h.name}** · {h.agency} · {place_text(h)} · "
                            f"{age_text(h.min_age, h.max_age)} · [상세]({h.url})")
                for e in h.evidence:
                    st.caption(f"({e.field}) {e.text[:200]}")
