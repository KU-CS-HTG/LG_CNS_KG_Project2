"""[C. UI] Streamlit 데모

실행:
    streamlit run app.py
"""
import streamlit as st

from src.generator import generate
from src.retriever import search

REGIONS = ["서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
           "대전광역시", "울산광역시", "세종특별자치시", "경기도", "강원특별자치도",
           "충청북도", "충청남도", "전북특별자치도", "전라남도", "경상북도",
           "경상남도", "제주특별자치도"]

st.set_page_config(page_title="정부 지원제도 맞춤 안내", page_icon="🧾")
st.title("🧾 나에게 맞는 정부 지원제도")

with st.sidebar:
    st.header("내 정보")
    age = st.number_input("나이", min_value=0, max_value=120, value=25)
    region = st.selectbox("거주지", REGIONS)

question = st.text_input("궁금한 내용", placeholder="예: 취업 준비 중인데 받을 수 있는 지원금 있어?")

if st.button("찾아보기") and question:
    user = {"age": int(age), "region": region}
    try:
        with st.spinner("검색 중..."):
            docs = search(question, user)
            answer = generate(question, user, docs)
    except Exception as e:  # 키 누락, 벡터DB 미생성 등을 화면에 표시
        st.error(f"오류: {e}")
    else:
        st.markdown(answer)
        with st.expander(f"근거 문서 {len(docs)}건"):
            for d in docs:
                st.markdown(f"**{d.metadata['name']}** · {d.metadata['agency']} "
                            f"· {d.metadata['min_age']}~{d.metadata['max_age']}세 · {d.metadata['region']}")
