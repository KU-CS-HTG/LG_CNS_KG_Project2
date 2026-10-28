"""예상 수령액 규칙 관리: 수집 원문에 금액이 없어 사람이 공식 자료를 보고 넣어야 하는 제도를 보고, 입력하면 답변 화면이 어떻게 바뀌는지 바로 확인한다.

실행: python -m app.amount_mode   (내부에서 `streamlit run`으로 다시 띄운다)
흐름: 표에서 제도 선택 -> ① 지금 답변에 나오는 모습 -> ② 답변 구성(카드 문구·자격 체크리스트·금액 안내와 공식 출처를 직접 씀, 선택으로 계산 규칙) ->
③ 입력 후 모습(저장 전 미리보기, 입력하는 대로 바뀜) -> 저장하면 이 제도가 실제 답변에 나올 때 그 구성으로 나온다 (src/generate/answer_override.py).
저장한 구성·규칙은 앱을 다시 띄우지 않아도 다음 질문부터 쓰인다 (파일 수정 시각을 보고 다시 읽음).
미리보기는 실제 카드(app.main._card)를 그대로 그리되 LLM·검색은 부르지 않는다 (지원 유형 정리 단계만 원문 첫 줄로 대신하므로 실제 항목 이름은 다를 수 있다).
app/main.py와 독립된 화면이다. 규칙 형식·검증은 src/generate/amount_manual.py, 계산은 amount_rules.py."""
from __future__ import annotations

import contextlib
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
from src.generate import amount_candidates, amount_from_url, amount_manual, amount_rules, answer_override, benefit_tiers, page_fetch  # noqa: E402

SHOW_ROWS = 500
SECTIONS = (("지원대상", "support_target"), ("선정기준", "selection_criteria"), ("지원내용", "support_content"), ("신청방법", "how_to_apply"))
STATES = ("아직 처리 안 한 제도", "AI가 채운 제도 (확인 필요)", "처리한 제도만 (다시 수정)", "전체")
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


def _preview_card(service: dict, base_needs: list[str], tag: str, rule: dict | None = None, draft: dict | None = None) -> None:
    """실제 카드를 그린다. tag='after'면 저장 전 구성(draft)·규칙(rule)을 쓴 것처럼 보여 준다. 결과 칸은 처음부터 열어 둔다."""
    sid = service["service_id"]
    key = f"pv{tag}_{sid}"
    with contextlib.ExitStack() as stack:
        if tag == "after":
            stack.enter_context(answer_override.preview(sid, draft))
            if rule is not None:
                stack.enter_context(amount_rules.preview_rule(rule))
        ov = answer_override.find(sid)
        needs = list(ov["needs_check"]) if ov and ov.get("needs_check") else base_needs      # 카드가 실제로 그릴 체크리스트
        r = amount_manual.preview_recommendation(service, needs)
        benefit_tiers._cache[(sid, tuple(needs))] = amount_manual.preview_tiers(service)       # LLM을 부르지 않는다
        st.session_state[f"tiers_open_{key}"] = True
        if needs:
            st.session_state[f"checks_done_{key}"] = card_benefit._checked(key, len(needs))      # '체크 완료'를 누른 것으로 본다
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


def _form(sid: str) -> list[dict]:
    """계산 규칙의 지원 유형 입력 (출처는 ②의 공식 출처 칸을 같이 쓴다)."""
    n = int(st.number_input("지원 유형 수 (금액·조건이 다른 지원이 몇 가지인가요?)", min_value=1, max_value=amount_manual.MAX_VARIANTS, value=1, key=f"am_{sid}_n"))
    return [_variant_form(sid, i) for i in range(1, n + 1)]


def _source_fields(sid: str, saved: dict) -> dict:
    st.markdown("**공식 출처** (금액 안내·계산 규칙에는 필수: 이 값을 어디서 읽었는지 남겨요)")
    title = st.text_input("자료 제목 (예: 2026년 사업 안내)", key=f"am_{sid}_stitle", **_init(f"am_{sid}_stitle", saved.get("title", "")))
    url = st.text_input("자료 주소 (https://…)", key=f"am_{sid}_surl", **_init(f"am_{sid}_surl", saved.get("url", "")))
    levels = list(LEVEL_LABELS)
    level_default = next((i for i, k in enumerate(levels) if LEVEL_LABELS[k] == saved.get("level")), 0)
    level = LEVEL_LABELS[st.radio("자료 등급", levels, horizontal=True, key=f"am_{sid}_level", **({} if f"am_{sid}_level" in st.session_state else {"index": level_default}))]
    quote = st.text_area("금액·조건이 적힌 구절 (자료에서 그대로 복사)", key=f"am_{sid}_quote", height=100, **_init(f"am_{sid}_quote", saved.get("quote", "")))
    return {"title": title, "url": url, "level": level, "quote": quote}


def _editor_hint(editor: str) -> None:
    """버튼이 꺼져 있는 이유를 알려 준다. 입력칸은 Enter를 누르거나 다른 곳을 눌러야 값이 적용된다."""
    if not editor:
        st.warning("버튼을 쓰려면 왼쪽 사이드바 '입력자 이름'에 이름을 입력하고 **Enter**를 누르세요. (사이드바가 접혀 있으면 왼쪽 위 »를 눌러 여세요.)")
    elif not amount_manual.valid_editor(editor):
        st.warning("입력자 이름은 영문·숫자·_-만 쓸 수 있어요 (예: taegyu). 한글은 쓸 수 없어요.")


def _review_controls(sid: str, editor: str, done: dict | None) -> None:
    """'지금 답변 그대로 OK'(규칙이 필요 없음)를 기록하면 목록에서 빠진다. 기록은 입력자별 파일에 쌓인다."""
    _editor_hint(editor)
    if done:
        st.success(f"확인 완료 — {done['by']} · {done['at'][:10]}" + (f" · {done['note']}" if done.get("note") else ""))
        if st.button("다시 확인 필요로 되돌리기", key=f"am_{sid}_undo", disabled=not amount_manual.valid_editor(editor)):
            amount_manual.mark_reviewed(sid, editor, undo=True)
            st.rerun()
        return
    c1, c2 = st.columns([3, 2])
    note = c1.text_input("확인 메모 (선택: 예 '현물 서비스라 금액 없음')", key=f"am_{sid}_rnote")
    c2.write("")
    if c2.button("✅ 지금 답변 그대로 OK (목록에서 숨기기)", key=f"am_{sid}_ok", disabled=not amount_manual.valid_editor(editor)):
        amount_manual.mark_reviewed(sid, editor, note)
        st.rerun()


def _amount_text(v: dict) -> tuple[str, str]:
    """계산 규칙용 폼 값(유형) -> 금액 안내의 (금액 문구, 조건 문구)."""
    if v["pay_type"] == "percent":
        text = f"비용의 {v['rate_pct']:g}%" + (f", 최대 {amount_rules.won(int(v['cap_won']))}" if v["cap_won"] else "")
    else:
        text = amount_rules.won(int(v["amount_won"])) + (f" × {v['months']}개월" if v["months"] else "")
    conds = ([f"만 {v['age_min'] or 0}~{v['age_max']}세" if v["age_max"] else f"만 {v['age_min']}세 이상"] if v["age_min"] or v["age_max"] else []) \
        + ([f"기준 중위소득 {v['median_pct']}% 이하"] if v["median_pct"] else []) + (["무주택"] if v["no_house"] else [])
    return text, ", ".join(conds)


def _apply_fill(sid: str, form: dict) -> None:
    """AI가 채운 값을 입력 위젯에 넣는다 (위젯을 그리기 전에 불러야 한다): 금액 안내·공식 출처·메모와 계산 규칙의 유형."""
    pay_label = {v: k for k, v in PAY_TYPES.items()}
    level_label = {v: k for k, v in LEVEL_LABELS.items()}
    st.session_state[f"am_{sid}_n"] = len(form["variants"])
    st.session_state[f"am_{sid}_na"] = len(form["variants"])
    for i, v in enumerate(form["variants"], 1):
        p = f"am_{sid}_{i}"
        st.session_state.update({f"{p}_name": v["name"], f"{p}_pay": pay_label[v["pay_type"]], f"{p}_amount": int(v["amount_won"]),
                                 f"{p}_rate": float(v["rate_pct"]), f"{p}_cap": int(v["cap_won"]), f"{p}_months": int(v["months"]),
                                 f"{p}_amin": int(v["age_min"]), f"{p}_amax": int(v["age_max"]), f"{p}_median": int(v["median_pct"]),
                                 f"{p}_house": bool(v["no_house"])})
        text, cond = _amount_text(v)
        st.session_state.update({f"am_{sid}_a{i}_label": v["name"], f"am_{sid}_a{i}_text": text, f"am_{sid}_a{i}_cond": cond})
    src = form["source"]
    st.session_state.update({f"am_{sid}_stitle": src["title"], f"am_{sid}_surl": src["url"], f"am_{sid}_level": level_label[src["level"]],
                             f"am_{sid}_quote": src["quote"]})
    if form.get("note"):
        st.session_state[f"am_{sid}_notes"] = form["note"]


def _take_result(sid: str, result: dict, url: str) -> None:
    st.session_state[f"am_{sid}_aires"] = {**{k: result.get(k) for k in ("ok", "message", "issues", "title", "text")}, "url": url}
    if result["ok"]:
        _apply_fill(sid, result["form"])


def _autofill(sid: str, service: dict) -> None:
    """공식 자료 주소를 넣으면 AI가 본문에서 값을 찾아 아래 폼을 채운다. 사람은 자료와 대조해 세부 내용만 고친다.
    접속이 막히는 사이트(네트워크 제한·접근 차단·보안문자·로그인)는 본문을 직접 붙여넣어 같은 방식으로 채운다."""
    st.markdown("**🤖 공식 자료 주소로 폼 채우기** (선택)")
    c1, c2 = st.columns([5, 1.4])
    url = c1.text_input("공식 자료 주소", key=f"am_{sid}_aiurl", placeholder="https://… (제도를 설명하는 공식 페이지)", label_visibility="collapsed")
    if c2.button("자료에서 채우기", key=f"am_{sid}_aibtn", disabled=not url.strip()):
        with st.spinner("자료를 읽고 값을 찾는 중… (몇 초 걸려요)"):
            result = amount_from_url.fill_form(service, url)
        _take_result(sid, result, url)
    with st.expander("접속이 안 되는 사이트라면 — 본문을 직접 붙여넣기"):
        st.caption("브라우저에서 그 페이지를 열어 본문을 복사해 붙여넣으면 같은 방식으로 채워요 (접속 제한·보안문자·로그인이 필요한 페이지도 돼요). 위 '공식 자료 주소'가 출처로 남아요.")
        pasted_title = st.text_input("자료 제목", key=f"am_{sid}_aipt")
        pasted = st.text_area("본문 붙여넣기", key=f"am_{sid}_aip", height=160)
        if st.button("붙여넣은 본문으로 채우기", key=f"am_{sid}_aipb", disabled=not (url.strip() and pasted.strip())):
            with st.spinner("본문에서 값을 찾는 중…"):
                result = amount_from_url.fill_from_text(service, url, pasted_title.strip(), pasted)
            if result["ok"]:
                page_fetch.save_snapshot(page_fetch.page_from_text(url, pasted_title.strip(), pasted))      # 나중에 근거 구절을 대조할 수 있게 남긴다
            _take_result(sid, result, url)
    res = st.session_state.get(f"am_{sid}_aires")
    if not res:
        return
    if res["ok"]:
        st.info(f"🤖 {res['message']} 아래 값은 AI가 자료에서 채운 것이에요. 자료와 대조해 틀린 곳만 고쳐 주세요.")
    else:
        st.warning(res["message"])
    for issue in res["issues"]:
        st.warning(issue)
    if res.get("text"):
        with st.expander("AI가 읽은 자료 본문 (대조용)"):
            st.caption(f"{res.get('title') or ''} · {res['url']}")
            st.text(res["text"][:8000])


def _init(key: str, value) -> dict:
    """위젯 처음 값. 이미 session_state에 값이 있으면(AI 채우기·사용자 입력) 기본값을 또 주지 않는다 (Streamlit 경고 방지)."""
    return {} if key in st.session_state else {"value": value}


def _lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


LAYOUT_MODES = {"나란히 (좌우)": "side", "위아래": "stack"}
LAYOUT_ORDERS = {"무엇을 받나요 먼저": "benefit_first", "어떻게 신청하나요 먼저": "apply_first"}


def _layout_controls(sid: str, saved: dict) -> dict:
    """'무엇을 받나요'·'어떻게 신청하나요' 두 칸의 위치(배치·순서)와 크기(왼쪽 칸 너비). 기본(좌우 50:50)이면 저장하지 않는다."""
    st.markdown("**카드 배치** — 두 칸을 나란히 둘지, 순서와 너비를 정해요 (③에서 바로 확인).")
    c1, c2, c3 = st.columns([2, 2, 3])
    modes, orders = list(LAYOUT_MODES), list(LAYOUT_ORDERS)
    mode_key, order_key, left_key = f"am_{sid}_lmode", f"am_{sid}_lorder", f"am_{sid}_lleft"
    mode = LAYOUT_MODES[c1.radio("배치", modes, key=mode_key, **({} if mode_key in st.session_state else {
        "index": next((i for i, k in enumerate(modes) if LAYOUT_MODES[k] == saved.get("mode")), 0)}))]
    order = LAYOUT_ORDERS[c2.radio("먼저 보일 칸", orders, key=order_key, **({} if order_key in st.session_state else {
        "index": next((i for i, k in enumerate(orders) if LAYOUT_ORDERS[k] == saved.get("order")), 0)}))]
    left = int(c3.slider("왼쪽 칸 너비 (%) — 오른쪽 칸은 나머지", answer_override.MIN_LEFT, answer_override.MAX_LEFT, step=5, key=left_key,
                         disabled=mode == "stack", **_init(left_key, int(saved.get("left_pct") or 50))))
    return {"mode": mode, "order": order, "left_pct": 50 if mode == "stack" else left}


def _compose(sid: str, service: dict, base_needs: list[str], saved: dict) -> tuple[dict, dict]:
    """② 답변 구성 입력 -> (override용 입력, 계산 규칙 폼). 기본값과 같은 칸은 '바꾸지 않음'(빈 값)으로 둔다."""
    base = amount_manual.preview_recommendation(service, [])
    st.markdown("**카드 문구** — 고친 칸만 실제 답변에 반영돼요 (그대로 두면 원래 문구).")
    benefit_default = saved.get("benefit_text") or base.benefit
    benefit = st.text_area("🎁 무엇을 받나요", key=f"am_{sid}_benefit", height=100, **_init(f"am_{sid}_benefit", benefit_default))
    how_default = saved.get("how_to_apply") or base.how_to_apply
    how = st.text_area("📝 어떻게 신청하나요", key=f"am_{sid}_how", height=80, **_init(f"am_{sid}_how", how_default))
    layout = _layout_controls(sid, saved.get("layout") or {})
    needs_default = "\n".join(saved.get("needs_check") or base_needs)
    needs = st.text_area("✅ 자격 체크리스트 (한 줄에 하나. 예: 주택 — 무주택 청년)", key=f"am_{sid}_cneeds", height=100, **_init(f"am_{sid}_cneeds", needs_default))

    st.markdown("**📋 지원 내용 줄** — '체크한 조건으로 받을 수 있는 지원' 아래에 나오는 문장이에요. 잘렸거나 마음에 안 들면 고쳐 쓰세요.")
    st.caption("실제 답변에서는 AI가 원문을 정리해 이 줄을 만들어서, 아래 기본값과 조금 다를 수 있어요. 여기에 쓴 내용이 있으면 그 내용으로 바뀌어요.")
    default_support = amount_manual.default_support_lines(service)
    saved_support = saved.get("support_lines") or default_support
    n_sup = int(st.number_input("지원 내용 줄 개수", min_value=0, max_value=answer_override.MAX_SUPPORT, key=f"am_{sid}_ns", **_init(f"am_{sid}_ns", len(saved_support))))
    support = []
    for i in range(1, n_sup + 1):
        old = saved_support[i - 1] if i <= len(saved_support) else {}
        c1, c2 = st.columns([2, 6])
        support.append({"label": c1.text_input(f"줄 {i} 이름", key=f"am_{sid}_s{i}_label", **_init(f"am_{sid}_s{i}_label", old.get("label", ""))),
                        "text": c2.text_area(f"줄 {i} 내용", key=f"am_{sid}_s{i}_text", height=90, **_init(f"am_{sid}_s{i}_text", old.get("text", "")))})

    st.markdown("**💰 금액 안내** — 사용자에게 보여 줄 금액을 직접 써요 (공식 출처 필수).")
    _autofill(sid, service)
    amount_title = st.text_input("금액 안내 제목 (카드에 나오는 소제목)", key=f"am_{sid}_atitle",
                                 **_init(f"am_{sid}_atitle", saved.get("amount_title") or answer_override.DEFAULT_AMOUNT_TITLE))
    saved_amounts = saved.get("amounts") or []
    n_amt = int(st.number_input("금액 안내 개수", min_value=0, max_value=answer_override.MAX_AMOUNTS, key=f"am_{sid}_na", **_init(f"am_{sid}_na", len(saved_amounts))))
    amounts = []
    for i in range(1, n_amt + 1):
        old = saved_amounts[i - 1] if i <= len(saved_amounts) else {}
        c1, c2, c3 = st.columns([2, 3, 3])
        amounts.append({"label": c1.text_input(f"항목 {i} 이름", key=f"am_{sid}_a{i}_label", **_init(f"am_{sid}_a{i}_label", old.get("label", ""))),
                        "text": c2.text_input(f"항목 {i} 금액 문구 (예: 보증 한도 최대 30억원)", key=f"am_{sid}_a{i}_text", **_init(f"am_{sid}_a{i}_text", old.get("text", ""))),
                        "condition": c3.text_input(f"항목 {i} 조건 (선택)", key=f"am_{sid}_a{i}_cond", **_init(f"am_{sid}_a{i}_cond", old.get("condition", "")))})
    source = _source_fields(sid, saved.get("source") or {})
    notes = st.text_area("안내 메모 (선택, 한 줄에 하나: 예 '선발형이라 예산에 따라 탈락할 수 있음')", key=f"am_{sid}_notes", height=70,
                         **_init(f"am_{sid}_notes", "\n".join(saved.get("notes") or [])))
    raw = {"benefit_text": benefit.strip() if benefit.strip() != base.benefit.strip() else "",
           "how_to_apply": how.strip() if how.strip() != base.how_to_apply.strip() else "",
           "needs_check": _lines(needs) if _lines(needs) != base_needs else [],
           "amounts": amounts, "notes": _lines(notes), "source": source, "layout": layout,
           "support_lines": support if support != default_support else [],
           "amount_title": amount_title.strip() if amount_title.strip() != answer_override.DEFAULT_AMOUNT_TITLE else ""}
    return raw, source


def _save_buttons(sid: str, editor: str, draft: dict | None, rule: dict | None, saved: dict | None) -> None:
    ok = amount_manual.valid_editor(editor)
    c1, c2 = st.columns([3, 2])
    if c1.button("이 구성을 실제 답변으로 저장", key=f"am_{sid}_save", disabled=not ok or not (draft or rule), type="primary"):
        try:
            if draft:
                answer_override.save(sid, editor, draft)
            outcome = amount_manual.save_rule(rule) if rule else None
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success("저장했어요. 이제 이 제도가 답변에 나오면 이 구성으로 나와요 (앱을 다시 띄울 필요 없어요)"
                       + (f" · 계산 규칙을 {'바꿨어요' if outcome == 'replaced' else '추가했어요'}" if outcome else "")
                       + ". 변경 파일(src/generate/answer_overrides/, amount_rules.json)을 git으로 올려 공유하세요.")
    if saved and c2.button("저장한 구성 삭제 (원래 답변으로)", key=f"am_{sid}_clear", disabled=not ok):
        answer_override.clear(sid, editor)
        st.rerun()


def _ai_review(sid: str, saved: dict, editor: str) -> None:
    """AI가 채운 구성: 확인하기 전에는 실제 답변에 나오지 않는다. 조사 메모(후보 값·출처)는 여기서만 보인다."""
    st.warning(f"🤖 AI({saved.get('by', '')})가 채운 내용이에요 — **아직 실제 답변에는 나오지 않아요.** 아래 ②·③을 확인해 고친 뒤 '저장'하거나, 그대로 괜찮으면 '확인 완료'를 누르세요.")
    if saved.get("ai_memo"):
        with st.expander("🤖 AI 조사 메모 (후보 값·출처, 사용자 화면에는 안 나와요)", expanded=True):
            st.text(saved["ai_memo"])
    if st.button("✅ AI가 채운 내용 확인 완료 (실제 답변에 반영)", key=f"am_{sid}_confirm", disabled=not amount_manual.valid_editor(editor)):
        answer_override.confirm(sid, editor)
        st.rerun()


def _detail(service: dict, editor: str, done: dict | None = None) -> None:
    sid = service["service_id"]
    st.session_state.setdefault("checks", {})
    st.session_state.setdefault("guides", {})
    existing = amount_rules.find(sid)
    saved = answer_override.find_any(sid)
    if existing:
        st.info(f"이미 계산 규칙이 있어요 · 확인일 {existing['as_of']} · {existing['status']}"
                + ("" if existing["calculator"] == "generic" else " · 코드로 계산하는 규칙이라 이 화면에서는 바꿀 수 없어요."))
    if answer_override.pending_ai(saved):
        _ai_review(sid, saved, editor)
    elif saved:
        st.info(f"직접 구성한 답변이 저장돼 있어요 · {saved.get('by', '')} · {(saved.get('at') or '')[:10]}"
                + (f" (AI가 채운 내용을 {saved['confirmed_by']}가 확인했어요)" if saved.get("confirmed_by") else "") + " (아래 ②에 불러왔어요)")
    with st.expander("제도 세부 내용 (수집 원문)", expanded=True):
        _content(service)
    _review_controls(sid, editor, done)

    st.subheader("① 지금 답변에 나오는 모습")
    st.caption("이 제도가 답변으로 제시될 때 사용자가 보는 카드예요 (저장한 구성이 있으면 그 내용이에요). 지원 유형 정리는 원문 첫 줄로 대신한 시뮬레이션이에요.")
    raw_needs = st.text_area("지금 답변의 자격 체크리스트 (비교용, 선택. 한 줄에 하나)", key=f"am_{sid}_needs", height=70)
    base_needs = _lines(raw_needs)
    _preview_card(service, base_needs, "before")

    st.subheader("② 답변 구성")
    st.caption("카드에 나올 문구와 금액을 직접 쓰세요. 입력하는 대로 아래 ③이 바로 바뀌고, 저장하면 실제 답변이 돼요.")
    raw, source = _compose(sid, service, base_needs, saved or {})
    use_calc = False
    rule, rule_problems = None, []
    with st.expander("🧮 금액을 입력값으로 계산해서 보여 주고 싶다면 (선택)", expanded=bool(existing)):
        st.caption("나이·소득 등을 입력하면 예상 수령액을 계산해 주는 규칙이에요. 위 공식 출처를 같이 써요.")
        use_calc = st.checkbox("계산 규칙도 저장하기", key=f"am_{sid}_usecalc", **_init(f"am_{sid}_usecalc", bool(existing and existing["calculator"] == "generic")))
        if use_calc:
            variants = _form(sid)
            rule, rule_problems = amount_manual.form_to_rule(service, {"variants": variants, "source": source, "note": raw["notes"][0] if raw["notes"] else ""}, editor)
            for p in rule_problems:
                st.warning(p)

    draft, problems = answer_override.validate(raw)
    only_empty = problems == ["바꿀 내용을 하나 이상 써 주세요."]

    st.subheader("③ 입력 후 모습 (저장 전 미리보기)")
    if draft is None and rule is None:
        if only_empty:
            st.info("아직 바꾼 내용이 없어요. 위 ②에서 문구나 금액을 고치면 여기에 바로 보여요.")
        else:
            st.warning("미리보려면 아래를 마무리해 주세요:\n\n" + "\n".join(f"- {p}" for p in problems))
        return
    for p in ([] if draft is not None else problems):
        st.warning(p)
    st.caption("카드 안 '내 조건으로 받을 수 있는 지원 보기'에서 금액 안내와 근거(자세히 보기)를 확인하세요.")
    _preview_card(service, base_needs, "after", rule, draft)
    with st.expander("저장될 내용 (JSON)"):
        st.code(json.dumps({"답변 구성": draft, "계산 규칙": rule}, ensure_ascii=False, indent=2), language="json")
    _save_buttons(sid, editor, draft, rule, saved)


def render() -> None:
    st.markdown(app_main.CSS, unsafe_allow_html=True)
    st.title("예상 수령액 규칙 관리")
    with st.sidebar:
        editor = st.text_input("입력자 이름 (영문·숫자, 입력 후 Enter)", placeholder="예: taegyu", key="amount_editor").strip()
        scope_label = st.radio("보여줄 제도", ["금액이 원문에 없는 제도", "모든 제도"], key="am_scope")
        include_local = st.checkbox("지자체 제도도 보기", key="am_local")
        state_label = st.radio("처리 상태", list(STATES), key="am_state",
                               help="처리 = 규칙 저장 · 답변 구성 저장 · 확인 완료. 'AI가 채운 제도'는 AI가 초안을 써 둔 제도로, 확인 완료하거나 고쳐서 저장하기 전에는 실제 답변에 나오지 않아요.")
        text = st.text_input("검색 (서비스명·소관기관·ID)", key="am_q")
        st.caption("수요 = 검수 기록에서 '좋음·부분'으로 판정된 횟수예요. 수요가 많은 제도부터 규칙을 만드세요.")
    services = _services()
    if not services:
        st.warning("제도 데이터를 읽지 못했어요 (data/services.db 또는 data/raw/serviceList.json). python -m src.datapack install 로 데이터 팩을 설치하세요.")
        return
    done_map = amount_manual.reviewed()
    rows = amount_manual.needs_official(list(services.values()), _demand(), scope="no_amount" if scope_label.startswith("금액이") else "all",
                                        include_local=include_local, text=text, done=set(done_map),
                                        show_done=state_label == STATES[3], only_done=state_label == STATES[2], only_ai=state_label == STATES[1])
    st.caption(("AI가 채워 확인을 기다리는 제도 " if state_label == STATES[1] else "처리한 제도 " if state_label == STATES[2] else "사람이 공식 자료를 넣어야 하는 제도 ")
               + f"{len(rows):,}개" + (f" (앞 {SHOW_ROWS}개만 표시)" if len(rows) > SHOW_ROWS else "")
               + " · 표에서 제도를 클릭하면 아래가 열려요")
    view = rows[:SHOW_ROWS]
    table = [{"ID": r["service_id"], "서비스명": r["service_name"], "소관기관": r["agency"], "수요": r["demand"],
              "상태": r["state"], "사유": r["reason"]} for r in view]
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
    if current not in {r["service_id"] for r in view}:      # 처리해서 목록에서 빠진 제도는 선택을 푼다
        current = None
    st.session_state["amount_sel_id"] = current
    st.divider()
    if not current or current not in services:
        st.info("조건에 맞는 제도가 없어요." if not view else "위 표에서 제도를 클릭하면 내용과 답변 화면을 볼 수 있어요.")
        return
    _detail(services[current], editor, done_map.get(current))


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
