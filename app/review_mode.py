"""검수 모드: 질문마다 후보 제도를 판정하고, 시스템 답변에 메모를 남긴다 (B 작성, C의 app/main.py와는 독립된 화면).

실행: python -m app.review_mode   (내부에서 `streamlit run`으로 다시 띄운다)
결과는 reviews/events/<검수자>.jsonl 에 추가만 된다 (설계: docs/REVIEW_STORE.md). 색인과 data/services.db가 필요하다.
app/main.py에 붙이려면 main()에서 `review_mode.render()`를 호출하면 된다."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from streamlit import runtime  # noqa: E402

from src.common import config  # noqa: E402
from src.review import schema, session  # noqa: E402
from src.review.cli import hybrid_search_fn, load_services  # noqa: E402
from src.review.store import ReviewStore  # noqa: E402

DEFAULT_CANDIDATES = 10      # 질문당 후보 수 기본값 (화면 왼쪽 슬라이더로 바꾼다)
MAX_CANDIDATES = 30
FIELDS = [("지원대상", "support_target"), ("선정기준", "selection_criteria"), ("지원내용", "support_content"),
          ("신청방법", "how_to_apply"), ("신청기한", "application_period")]
MARK = {"good": "🟢", "partial": "🟡", "bad": "🔴", "unsure": "⚪"}


@st.cache_resource(show_spinner="제도 데이터를 읽는 중…")
def _services() -> dict[str, dict]:
    return load_services(config.DB_PATH)


def _hits(qid: str, text: str, filters: dict, depth: int) -> list[tuple[str, int, str]]:
    """질문당 한 번만 검색해 두고 화면을 다시 그려도 재사용한다 (후보 수를 바꾸면 다시 검색)."""
    cache = st.session_state.setdefault("review_hits", {})
    if (qid, depth) not in cache:
        with st.spinner("후보를 찾는 중…"):
            cache[(qid, depth)] = hybrid_search_fn()(text, filters, depth)
    return cache[(qid, depth)]


def _card(store: ReviewStore, reviewer: str, qid: str, cand: dict, run_id_for) -> None:
    service, sid = cand["service"], cand["service_id"]
    mine, consensus = cand["mine"], cand["consensus"]
    badge = MARK.get(mine["data"]["label"], "") if mine else "⬜"
    with st.expander(f"{badge} {service.get('service_name', sid)}  ·  {service.get('agency', '')}", expanded=mine is None):
        for title, key in FIELDS:
            if service.get(key):
                st.markdown(f"**{title}**  \n{service[key]}")
        if service.get("detail_url"):
            st.markdown(f"[상세 보기]({service['detail_url']})")
        if consensus["status"] in ("conflict", "stale"):
            st.warning({"conflict": "검수자 사이에 판정이 엇갈려 있어요.", "stale": "제도 내용이 바뀌어 이전 판정이 무효예요. 다시 판정해 주세요."}[consensus["status"]])
        current = mine["data"] if mine and consensus["status"] != "stale" else {}
        labels = list(schema.LABELS)
        label = st.radio("이 질문에 이 제도가 맞나요?", labels, index=labels.index(current["label"]) if current.get("label") in labels else None,
                         format_func=session.LABEL_TEXT.get, key=f"label:{qid}:{sid}", horizontal=True)
        reasons = session.reasons_for(label) if label else ()
        reason = None
        if reasons:
            default = reasons.index(current["reason"]) if current.get("reason") in reasons else None
            reason = st.selectbox("사유", reasons, index=default, format_func=session.REASON_TEXT.get, placeholder="사유를 고르세요",
                                  key=f"reason:{qid}:{sid}")
        note = st.text_input("메모 (선택, 사유가 '기타'면 필수)", value=current.get("note") or "", max_chars=schema.MAX_NOTE, key=f"note:{qid}:{sid}")
        if st.button("저장", key=f"save:{qid}:{sid}", disabled=label is None):
            try:
                session.save_judgment(store, reviewer, qid, service, label, reason=reason, note=note, run_id=run_id_for())
            except schema.ValidationError as exc:
                st.error(f"저장하지 못했어요: {exc} (사유와 메모를 확인해 주세요)")
            else:
                st.rerun()


def _answer_review(store: ReviewStore, reviewer: str, qid: str, text: str) -> None:
    st.subheader("시스템 답변 검수")
    st.caption("답변을 만들어 읽고, 빠진 내용·틀린 내용을 메모로 남깁니다. 메모는 나중에 팀이 검토해 지침이 됩니다.")
    key = f"answer:{qid}"
    if st.button("현재 시스템 답변 만들기", key=f"gen:{qid}"):
        from src.generate import generator          # 무거운 모듈이라 필요할 때만

        with st.spinner("답변을 만드는 중… (10~20초)"):
            try:
                st.session_state[key] = generator.answer_question(text).text
            except Exception as exc:                # noqa: BLE001 — 화면에 원인을 보여 주고 계속 쓸 수 있게
                st.error(f"답변을 만들지 못했어요: {exc!r}")
    answer = st.session_state.get(key)
    if not answer:
        return
    st.markdown(answer)
    with st.form(f"memo:{qid}", clear_on_submit=True):
        kind = st.selectbox("메모 종류", list(session.MEMO_TEXT), format_func=session.MEMO_TEXT.get)
        severity = st.radio("심각도", list(schema.SEVERITIES), format_func={"minor": "가벼움", "major": "중요"}.get, horizontal=True)
        memo = st.text_area("메모", max_chars=schema.MAX_TEXT)
        if st.form_submit_button("메모 저장"):
            if not memo.strip():
                st.error("메모를 적어 주세요.")
            else:
                session.save_answer_review(store, reviewer, qid, answer, [{"kind": kind, "text": memo, "severity": severity}])
                st.success("저장했어요.")


def render() -> None:
    st.title("검수 모드")
    store = ReviewStore()
    with st.sidebar:
        reviewer = st.text_input("검수자 이름 (영문·숫자)", value=st.session_state.get("reviewer", ""), placeholder="예: taegyu").strip()
        split = st.selectbox("질문 묶음", ["dev", "test", "all"], format_func={"dev": "dev (개발용)", "test": "test (최종 시험용)", "all": "전체"}.get)
        limit = st.slider("질문당 후보 수", 3, MAX_CANDIDATES, DEFAULT_CANDIDATES, help="검색 방식 세 가지의 결과를 합쳐 순위가 좋은 순으로 이만큼만 보여 줘요. 이미 판정한 제도는 따로 보입니다.")
        today = st.date_input("기준일 (기한이 지난 제도는 숨김)", value=date.today()).isoformat()
        st.caption("판정은 reviews/events/<이름>.jsonl 에 쌓여요. git으로 올려 공유하세요.")
    if not reviewer:
        st.info("왼쪽에 검수자 이름을 먼저 입력해 주세요.")
        return
    if not schema._REVIEWER.match(reviewer):
        st.error("이름은 영문·숫자·_ . - 만 쓸 수 있어요 (40자 이내).")
        return
    st.session_state["reviewer"] = reviewer

    state = store.state()
    rows = session.questions_overview(state, reviewer, split)
    if not rows:
        st.warning("검수할 질문이 없어요. `python -m src.review import-questions`로 질문을 먼저 등록하세요.")
        return
    done = sum(1 for r in rows if r["mine"])
    st.progress(done / len(rows), text=f"내가 시작한 질문 {done} / {len(rows)}")
    options = {r["question_id"]: r for r in rows}
    qid = st.selectbox("질문", list(options), key="review_qid", format_func=lambda i: f"{'✅' if options[i]['mine'] else '⬜'} {options[i]['id']} · {options[i]['text'][:60]}")
    q = state.questions[qid]
    st.markdown(f"### {q['text']}")
    if q.get("gold_filters"):
        st.caption("기대 조건: " + ", ".join(f"{k}={v}" for k, v in q["gold_filters"].items()))

    try:
        services = _services()
        hits = _hits(qid, q["text"], q.get("gold_filters", {}), limit)
    except (SystemExit, Exception) as exc:           # noqa: BLE001 — DB·색인이 없을 때 안내
        st.error(f"후보를 가져오지 못했어요: {exc}")
        return
    cands = session.candidates_for(state, qid, lambda *_: hits, services, today=today, reviewer=reviewer, n=limit, limit=limit)
    st.caption(f"후보 {len(cands)}개 (순서는 섞여 있고, 어떤 검색이 찾았는지는 가려져 있어요). 이 질문에 맞는 제도가 없다면 모두 '나쁨'으로 판정하세요.")

    def run_id_for() -> str:                          # 첫 저장 때 한 번만 run을 남긴다
        key = f"run:{qid}"
        if key not in st.session_state:
            st.session_state[key] = session.start_run(store, reviewer, qid, cands)
        return st.session_state[key]

    for cand in cands:
        _card(store, reviewer, qid, cand, run_id_for)
    st.divider()
    _answer_review(store, reviewer, qid, q["text"])


def main() -> None:
    st.set_page_config(page_title="검수 모드", page_icon="📝", layout="centered")
    render()


if __name__ == "__main__":
    if runtime.exists():
        main()
    else:
        from streamlit.web import cli as stcli
        sys.argv = ["streamlit", "run", str(Path(__file__).resolve())]
        sys.exit(stcli.main())
