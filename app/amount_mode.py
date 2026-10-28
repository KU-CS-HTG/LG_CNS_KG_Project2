"""예상 수령액 규칙 관리: 수집 원문에 금액이 없어 사람이 공식 자료를 보고 넣어야 하는 제도를 보고, 입력하면 답변 화면이 어떻게 바뀌는지 바로 확인한다.

실행: python -m app.amount_mode   (내부에서 `streamlit run`으로 다시 띄운다)
흐름: 표에서 제도 선택 -> ① 지금 답변에 나오는 모습 -> ② 공식 자료(금액·조건·출처) 입력 -> ③ 입력 후 모습(저장 전 미리보기) -> 저장.
저장한 규칙은 앱을 다시 띄우지 않아도 다음 질문부터 쓰인다 (규칙 파일 수정 시각을 보고 다시 읽음).
미리보기는 실제 카드(app.main._card)를 그대로 그리되 LLM·검색은 부르지 않는다 (지원 유형 정리 단계만 원문 첫 줄로 대신하므로 실제 항목 이름은 다를 수 있다).
app/main.py와 독립된 화면이다. 규칙 형식·검증은 src/generate/amount_manual.py, 계산은 amount_rules.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from streamlit import runtime  # noqa: E402

from app import card_benefit  # noqa: E402
from app import main as app_main  # noqa: E402
from app.catalog_mode import pick_selected  # noqa: E402
from src.common import config  # noqa: E402
from src.generate import amount_candidates, amount_manual, amount_rules, benefit_tiers  # noqa: E402

SHOW_ROWS = 500
SECTIONS = (("지원대상", "support_target"), ("선정기준", "selection_criteria"), ("지원내용", "support_content"), ("신청방법", "how_to_apply"))
PAY_TYPES = {"정액 (정해진 금액을 지급)": "fixed", "비용의 일정 비율 (한도 있음)": "percent"}
LEVEL_LABELS = {"공식 (정부·공공기관 원문)": "공식", "2차 (신뢰할 만한 요약 자료)": "2차"}


@st.cache_resource(show_spinner="제도 데이터를 읽는 중…")
def _services() -> dict[str, dict]:
    """DB(활성 제도)를 읽고, 없으면 수집 원본(data/raw/serviceList.json)으로 대신한다."""
    try:
        from src.review.cli import load_services
        return load_services(config.DB_PATH)
    except SystemExit:
        raw = config.ROOT / "data" / "raw" / "serviceList.json"
        if not raw.is_file():
            return {}
        return {s["service_id"]: s for s in amount_candidates.load_raw(raw)}


@st.cache_data(show_spinner=False)
def _demand() -> dict[str, int]:
    return amount_candidates.demand_counts(config.ROOT / "reviews" / "events")


def _content(service: dict) -> None:
    st.markdown(f"**{service.get('service_name', '')}**  ·  `{service['service_id']}`  ·  {service.get('agency', '')}")
    if service.get("detail_url"):
        st.markdown(f"[상세 페이지 열기 ↗]({service['detail_url']})")
    for label, key in SECTIONS:
        text = (service.get(key) or "").strip()
        st.markdown(f"**{label}**")
        st.text(text) if text else st.caption("(비어 있음)")


def _preview_card(service: dict, needs: list[str], tag: str, rule: dict | None = None) -> None:
    """실제 카드를 그린다. rule이 있으면 저장 전 규칙을 쓴 것처럼 보여 준다. 결과 칸은 처음부터 열어 둔다."""
    sid = service["service_id"]
    key = f"pv{tag}_{sid}"
    r = amount_manual.preview_recommendation(service, needs)
    benefit_tiers._cache[(sid, tuple(needs))] = amount_manual.preview_tiers(service)       # LLM을 부르지 않는다
    st.session_state[f"tiers_open_{key}"] = True
    if needs:
        st.session_state[f"checks_done_{key}"] = card_benefit._checked(key, len(needs))      # '체크 완료'를 누른 것으로 본다
    if rule is None:
        app_main._card(1, r, key)
    else:
        with amount_rules.preview_rule(rule):
            app_main._card(1, r, key)


def _variant_form(sid: str, i: int) -> dict:
    p = f"am_{sid}_{i}"
    with st.container(border=True):
        st.markdown(f"**지원 유형 {i}**")
        name = st.text_input("유형 이름 (예: 청년 월세 지원)", key=f"{p}_name")
        pay = PAY_TYPES[st.radio("지급 방식", list(PAY_TYPES), horizontal=True, key=f"{p}_pay")]
        c1, c2, c3 = st.columns(3)
        v = {"name": name, "pay_type": pay}
        if pay == "fixed":
            v["amount_won"] = c1.number_input("금액 (원)", min_value=0, step=10_000, key=f"{p}_amount")
        else:
            v["rate_pct"] = c1.number_input("지원 비율 (%)", min_value=0.0, max_value=100.0, step=1.0, key=f"{p}_rate")
            v["cap_won"] = c2.number_input("한도 (원, 없으면 0)", min_value=0, step=10_000, key=f"{p}_cap")
        v["months"] = c3.number_input("지급 개월 수 (한 번만 받으면 0)", min_value=0, max_value=600, step=1, key=f"{p}_months")
        d1, d2, d3, d4 = st.columns(4)
        v["age_min"] = d1.number_input("나이 최소 (없으면 0)", min_value=0, max_value=120, key=f"{p}_amin")
        v["age_max"] = d2.number_input("나이 최대 (없으면 0)", min_value=0, max_value=120, key=f"{p}_amax")
        v["median_pct"] = d3.number_input("중위소득 N% 이하 (없으면 0)", min_value=0, max_value=1000, key=f"{p}_median")
        v["no_house"] = d4.checkbox("무주택 필요", key=f"{p}_house")
    return v


def _form(sid: str) -> dict:
    n = int(st.number_input("지원 유형 수 (금액·조건이 다른 지원이 몇 가지인가요?)", min_value=1, max_value=amount_manual.MAX_VARIANTS, value=1, key=f"am_{sid}_n"))
    variants = [_variant_form(sid, i) for i in range(1, n + 1)]
    st.markdown("**공식 출처** (필수: 이 값을 어디서 읽었는지 남겨요)")
    title = st.text_input("자료 제목 (예: 2026년 사업 안내)", key=f"am_{sid}_stitle")
    url = st.text_input("자료 주소 (https://…)", key=f"am_{sid}_surl")
    level = LEVEL_LABELS[st.radio("자료 등급", list(LEVEL_LABELS), horizontal=True, key=f"am_{sid}_level")]
    quote = st.text_area("금액·조건이 적힌 구절 (자료에서 그대로 복사)", key=f"am_{sid}_quote", height=100)
    note = st.text_input("남은 변수·메모 (선택: 예 '선발형이라 예산에 따라 탈락할 수 있음')", key=f"am_{sid}_note")
    return {"variants": variants, "source": {"title": title, "url": url, "level": level, "quote": quote}, "note": note}


def _detail(service: dict, editor: str) -> None:
    sid = service["service_id"]
    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    existing = amount_rules.find(sid)
    if existing:
        st.info(f"이미 규칙이 있어요 · 확인일 {existing['as_of']} · {existing['status']}"
                + ("" if existing["calculator"] == "generic" else " · 코드로 계산하는 규칙이라 이 화면에서는 바꿀 수 없어요."))
    with st.expander("제도 세부 내용 (수집 원문)", expanded=True):
        _content(service)

    st.subheader("① 지금 답변에 나오는 모습")
    st.caption("이 제도가 답변으로 제시될 때 사용자가 보는 카드예요. 지원 유형 정리는 원문 첫 줄로 대신한 시뮬레이션이에요.")
    raw_needs = st.text_area("카드의 자격 체크리스트 항목 (선택, 한 줄에 하나. 예: 주택 — 무주택 청년)", key=f"am_{sid}_needs", height=80)
    needs = [l.strip() for l in raw_needs.splitlines() if l.strip()]
    _preview_card(service, needs, "before")

    st.subheader("② 공식 자료 입력")
    st.caption("공식 자료에서 읽은 금액·조건을 입력하세요. 입력하는 대로 아래 ③이 바로 바뀌어요.")
    form = _form(sid)
    rule, problems = amount_manual.form_to_rule(service, form, editor)

    st.subheader("③ 입력 후 모습 (저장 전 미리보기)")
    if rule is None:
        st.info("아직 미리볼 수 없어요. 아래를 채워 주세요:\n\n" + "\n".join(f"- {p}" for p in problems))
        return
    st.caption("카드 안 '예상 수령액'에 값을 넣어 보면 사용자가 받게 될 계산 결과를 확인할 수 있어요.")
    _preview_card(service, needs, "after", rule)
    with st.expander("저장될 규칙 (JSON)"):
        st.code(json.dumps(rule, ensure_ascii=False, indent=2), language="json")
    if not editor:
        st.caption("왼쪽에 입력자 이름을 넣으면 저장할 수 있어요.")
    if st.button("이 규칙으로 저장", key=f"am_{sid}_save", disabled=not editor, type="primary"):
        try:
            outcome = amount_manual.save_rule(rule)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success(("기존 규칙을 바꿨어요" if outcome == "replaced" else "규칙을 추가했어요")
                       + ". 이제 이 제도가 답변에 나오면 입력한 규칙으로 계산해요 (앱을 다시 띄울 필요 없어요). 규칙 파일(src/generate/amount_rules.json)을 git으로 올려 공유하세요.")


def render() -> None:
    st.markdown(app_main.CSS, unsafe_allow_html=True)
    st.title("예상 수령액 규칙 관리")
    with st.sidebar:
        editor = st.text_input("입력자 이름 (영문·숫자)", value=st.session_state.get("amount_editor", ""), placeholder="예: taegyu").strip()
        st.session_state["amount_editor"] = editor
        scope_label = st.radio("보여줄 제도", ["금액이 원문에 없는 제도", "모든 제도"], key="am_scope")
        include_local = st.checkbox("지자체 제도도 보기", key="am_local")
        text = st.text_input("검색 (서비스명·소관기관·ID)", key="am_q")
        st.caption("수요 = 검수 기록에서 '좋음·부분'으로 판정된 횟수예요. 수요가 많은 제도부터 규칙을 만드세요.")
    services = _services()
    if not services:
        st.warning("제도 데이터를 읽지 못했어요 (data/services.db 또는 data/raw/serviceList.json). python -m src.datapack install 로 데이터 팩을 설치하세요.")
        return
    rows = amount_manual.needs_official(list(services.values()), _demand(), scope="no_amount" if scope_label.startswith("금액이") else "all",
                                        include_local=include_local, text=text)
    st.caption(f"사람이 공식 자료를 넣어야 하는 제도 {len(rows):,}개" + (f" (앞 {SHOW_ROWS}개만 표시)" if len(rows) > SHOW_ROWS else "")
               + " · 표에서 제도를 클릭하면 아래가 열려요")
    view = rows[:SHOW_ROWS]
    table = [{"ID": r["service_id"], "서비스명": r["service_name"], "소관기관": r["agency"], "수요": r["demand"],
              "규칙": "✅" if r["has_rule"] else "", "사유": r["reason"]} for r in view]
    current = st.session_state.get("amount_sel_id")
    try:
        event = st.dataframe(table, hide_index=True, height=300, key="am_table", on_select="rerun", selection_mode="single-row")
        current = pick_selected(list(event.selection.rows), view, current)
    except TypeError:
        st.dataframe(table, hide_index=True, height=300)
        if view:
            ids = [r["service_id"] for r in view]
            current = st.selectbox("제도 선택", ids, index=ids.index(current) if current in ids else 0, key="am_pick",
                                   format_func=lambda i: f"{services[i].get('service_name', '')}  ·  {i}")
    st.session_state["amount_sel_id"] = current
    st.divider()
    if not current or current not in services:
        st.info("조건에 맞는 제도가 없어요." if not view else "위 표에서 제도를 클릭하면 내용과 답변 화면을 볼 수 있어요.")
        return
    _detail(services[current], editor)


def main() -> None:
    st.set_page_config(page_title="예상 수령액 규칙 관리", page_icon="💰", layout="wide")
    render()


if __name__ == "__main__":
    if runtime.exists():
        main()
    else:
        from streamlit.web import cli as stcli
        sys.argv = ["streamlit", "run", str(Path(__file__).resolve())]
        raise SystemExit(stcli.main())
