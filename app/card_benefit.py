"""W2: 내 조건으로 받을 수 있는 지원 (카드의 자격 체크리스트 아래).

흐름: 사용자가 체크리스트를 체크하고 '체크 완료'를 누르면 `mark_done`이 그 시점의 체크 상태를 기억한다 -> 체크가 그대로면
'내 조건으로 받을 수 있는 지원 보기' 버튼이 나온다 -> 누르면 제도의 조건별 지원(`benefit_tiers`, LLM 한 번)을 정리해 체크한
조건에 맞는 지원과 금액을 보여 준다. 금액 근거(원문 구절)는 '자세히 보기'에서 확인한다.
금액 규칙이 있는 제도(`amount_rules`)는 입력값으로 예상 수령액을 계산해 보여 준다.
체크를 바꾸면 결과를 닫고 다시 '체크 완료'를 누르게 한다. 체크할 때마다 LLM을 부르지 않는다.
지원내용 원문은 카드 머리 '무엇을 받나요' 칸의 '원문 확인하기' 링크(새 창)로 본다 (app/main.py)."""
from __future__ import annotations

import re

import streamlit as st

from src.common.schema import Recommendation
from src.generate import amount_rules, answer_override, benefit_tiers, earned_credit_rules


def _checked(key: str, n: int) -> frozenset[int]:
    checks = st.session_state.get("checks", {})
    return frozenset(j + 1 for j in range(n) if checks.get(f"chk_{key}_{j}"))


def mark_done(key: str, n: int) -> None:
    """'체크 완료' 버튼 콜백: 지금 체크 상태를 확정으로 기억한다."""
    checks = st.session_state.setdefault("checks", {})
    for j in range(n):                       # 같은 클릭에서 바뀐 위젯 값까지 반영
        k = f"chk_{key}_{j}"
        if k in st.session_state:
            checks[k] = bool(st.session_state[k])
    st.session_state[f"checks_done_{key}"] = _checked(key, n)
    st.session_state[f"tiers_open_{key}"] = False


def _needs_text(r: Recommendation, numbers: list[int]) -> str:
    return ", ".join(r.needs_check[n - 1].split(" — ")[0] if 0 < n <= len(r.needs_check) else str(n) for n in numbers)


def _tier_line(t: dict, show_amount: bool = True) -> None:
    badge = "" if t["basis"] == benefit_tiers.SOURCE else "  ·  _자료 밖 정보(정확하지 않을 수 있어요)_"
    st.markdown(f"**{t['label']}** — {t['benefit']}{badge}")
    if t.get("amount"):
        st.markdown(f"💰 **받을 수 있는 금액: {t['amount']}**")
    elif show_amount:
        st.caption("💰 금액을 확인하지 못했어요. 담당 기관에서 확인해 주세요.")
    evidence = ([{"kind": "수집한 원문", "quote": t["quote"], "url": ""}] if t["amount_basis"] == benefit_tiers.SOURCE and t["quote"] else
                t.get("evidence") or [])
    if evidence:
        with st.expander("자세히 보기 (금액 근거)"):
            for e in evidence:
                link = f" — [출처 열기 ↗]({e['url']})" if e.get("url") else ""
                st.markdown(f"**{e['kind']}**{link}")
                st.text(e["quote"])


def _show_amount(out: dict) -> None:
    for sc in out["scenarios"]:
        icon = {"해당 가능": "✅", "확인 필요": "❓", "해당 어려움": "⛔", "대안": "🔁"}[sc["verdict"]]
        st.markdown(f"**{icon} {sc['title']} — {sc['verdict']}**")
        if sc["amount_won"] is not None:
            st.markdown(f"💰 **예상 수령액: {amount_rules.won(sc['amount_won'])}**")
        elif sc.get("amount_text"):
            st.markdown(f"💰 **받을 수 있는 금액: {sc['amount_text']}** _(2차 자료 기준)_")
        for line in sc["lines"]:
            st.markdown(f"- {line}")
        for reason in sc["reasons"]:
            st.caption(reason)
    for note in out["notes"]:
        st.warning(note)
    with st.expander("자세히 보기 (계산 근거·남은 변수)"):
        st.markdown("**남은 변수 (계산에 반영하지 못한 것)**")
        for text in out["open_issues"] + out["conflicts"]:
            st.markdown(f"- {text}")
        st.markdown("**근거 자료**")
        for src in out["sources"]:
            st.markdown(f"- [{src['title']}]({src['url']}) · {src['level']} — {src['facts']}")
        if out["general_track_note"]:
            st.caption(out["general_track_note"])
        st.caption(f"확인일 {out['as_of']} · 상태: {out['status']}. 계산 값은 참고용이며 실제 지급액은 담당 기관 심사로 정해져요.")


def _generic_form(r: Recommendation, rule: dict, key: str, checked: frozenset[int]) -> None:
    """범용 규칙(JSON만으로 만든 규칙): 체크리스트로 알 수 있는 입력은 채우고 나머지만 묻고, 값이 바뀔 때마다 바로 계산한다."""
    values = amount_rules.auto_flags(rule, list(r.needs_check), set(checked))
    if values:
        st.caption("체크리스트 답으로 채운 항목: " + ", ".join(f"{s['label']} → {'예' if values[s['id']] else '아니요'}" for s in rule["inputs"] if s["id"] in values))
    for spec in rule["inputs"]:
        if spec["id"] in values:
            continue
        wk, label, kind = f"amt_{key}_{spec['id']}", spec["label"], spec["type"]
        if kind == "bool":
            picked = st.radio(label, ["예", "아니요"], index=None, horizontal=True, key=wk)
            values[spec["id"]] = None if picked is None else picked == "예"
        elif kind == "choice":
            values[spec["id"]] = st.selectbox(label, spec["options"], index=None, key=wk)
        else:
            step = 1 if kind == "int" else 0.1
            values[spec["id"]] = st.number_input(label, min_value=spec.get("min"), max_value=spec.get("max"), value=None, step=step, key=wk)
    _show_amount(amount_rules.generic_calculate(rule, values))


def _amount_form(r: Recommendation, rule: dict, key: str, checked: frozenset[int]) -> None:
    """예상 수령액. 체크리스트 답으로 바로 계산하고(같은 내용을 다시 묻지 않는다), 체크로 알 수 없는 나이대·부양가족 수만 묻는다.
    소득·재산을 숫자로 넣어 단독 가구/가구 합산을 비교하는 정밀 계산은 선택 사항(접어 둔 칸)이다. LLM 없음."""
    st.markdown("##### 💰 예상 수령액")
    if rule["calculator"] == "generic":
        _generic_form(r, rule, key, checked)
        return
    flags = amount_rules.check_flags(rule, list(r.needs_check), set(checked))
    band = st.radio("나이대", amount_rules.AGE_BANDS, index=None, horizontal=True, key=f"amt_band_{key}")
    cols = st.columns(3)
    deps = sum(int(c.number_input(label, min_value=0, max_value=10, value=0, step=1, key=f"amt_{key}_{name}"))
               for c, (label, name) in zip(cols, (("만 18세 이하 가족 수", "minor"), ("만 70세 이상 가족 수", "senior"), ("중증장애인 가족 수", "disabled"))))
    _show_amount(amount_rules.from_checks(rule, flags, band, deps))
    with st.expander("소득·재산을 숫자로 넣어 더 자세히 계산하기 (선택)"):
        st.caption("단독 가구로 인정되는 경우와 가구원과 합산하는 경우를 나눠 비교해요.")
        state_key = f"amount_result_{key}"
        with st.form(f"amount_form_{key}"):
            values = {}
            for spec in amount_rules.inputs_for(rule):
                wk = f"amt_{key}_{spec['id']}"
                if spec["type"] == "choice":
                    values[spec["id"]] = st.selectbox(spec["label"], spec["options"], index=spec["options"].index(spec["default"]), key=wk)
                elif spec["type"] == "float":
                    values[spec["id"]] = st.number_input(spec["label"], min_value=spec["min"], max_value=spec["max"], value=spec["default"], step=0.1, key=wk)
                else:
                    values[spec["id"]] = st.number_input(spec["label"], min_value=spec["min"], max_value=spec["max"], value=spec["default"], step=1, key=wk)
            submitted = st.form_submit_button("예상 금액 계산")
        if submitted:
            st.session_state[state_key] = amount_rules.calculate(rule, values)
        if st.session_state.get(state_key):
            _show_amount(st.session_state[state_key])


def _show_earned_credit(out: dict) -> None:
    for sec in (out["work"], out["child"]):
        if not sec:
            continue
        icon = {"해당 가능": "✅", "확인 필요": "❓", "해당 어려움": "⛔"}[sec["verdict"]]
        st.markdown(f"**{icon} {sec['title']} — {sec['verdict']}**")
        if sec["high_won"]:
            same = sec["low_won"] == sec["high_won"]
            st.markdown("💰 **예상 수령액: " + (amount_rules.won(sec["low_won"]) if same else f"{amount_rules.won(sec['low_won'])} ~ {amount_rules.won(sec['high_won'])}") + "**")
        for line in sec["lines"]:
            st.markdown(f"- {line}")
    for note in out["notes"]:
        st.caption(note)
    with st.expander("자세히 보기 (계산 근거·자료 간 불일치·남은 변수)"):
        st.markdown("**자료 간 불일치**")
        for text in out["conflicts"]:
            st.markdown(f"- {text}")
        st.markdown("**계산에 반영하지 못한 것**")
        for text in out["open_issues"]:
            st.markdown(f"- {text}")
        st.markdown("**근거 자료**")
        for src in out["sources"]:
            st.markdown(f"- [{src['title']}]({src['url']}) · {src['level']} — {src['facts']}")
    st.caption(f"확인일 {out['as_of']} · 상태: {out['status']}. 홈택스 모의계산과 다를 수 있어요. 최종 금액은 국세청 심사로 정해져요.")


def _earned_credit_form(r: Recommendation, rule: dict, key: str) -> None:
    """근로·자녀장려금: 가구원 구성·부양가족 수·총급여액을 넣으면 예상 수령액을 바로 계산한다 (값이 바뀔 때마다). LLM 없음."""
    st.markdown("##### 💰 예상 수령액 (근로·자녀장려금)")
    st.caption("총급여액은 근로소득만 기준이에요. 아래 값은 공식 모의계산(홈택스)과 대조 전 초안이에요.")
    values: dict = {}
    for spec in earned_credit_rules.INPUTS:
        if any(values.get(k) != v for k, v in (spec.get("when") or {}).items()):
            continue
        wk = f"ecr_{key}_{spec['id']}"
        if spec["type"] == "choice":
            values[spec["id"]] = st.radio(spec["label"], spec["options"], index=None, horizontal=True, key=wk)
        else:
            values[spec["id"]] = st.number_input(spec["label"], min_value=spec["min"], max_value=spec["max"], value=None, step=1, key=wk)
    if values.get("own_man") is None or values.get("spouse") is None:
        st.info("본인 총급여액과 배우자 유무를 입력하면 계산해요.")
        return
    _show_earned_credit(earned_credit_rules.calculate(rule, values))


def _override_amounts(ov: dict | None) -> bool:
    """담당자가 직접 쓴 금액 안내와 공식 근거. 그렸으면 True."""
    if not ov or not (ov.get("amounts") or ov.get("notes")):
        return False
    if ov.get("amounts"):
        st.markdown(f"##### 💰 {ov.get('amount_title') or answer_override.DEFAULT_AMOUNT_TITLE}")
        for a in ov["amounts"]:
            st.markdown(f"**{a['label']}** — {a['text']}")
            if a.get("condition"):
                st.caption(f"조건: {a['condition']}")
        src = ov.get("source") or {}
        with st.expander("자세히 보기 (금액 근거)"):
            st.markdown(f"[{src.get('title', '출처')}]({src.get('url', '')}) · {'공식 자료' if src.get('level') == '공식' else '2차 자료(공식 원문 대조 필요)'}")
            st.text(src.get("quote", ""))
            st.caption(f"담당자 {ov.get('by') or '-'} 확인" + (f" · {ov['at'][:10]}" if ov.get("at") else ""))
    for note in ov.get("notes") or []:
        st.caption(f"안내: {note}")
    return True


def _break_lines(text: str) -> str:
    """markdown은 한 번 줄바꿈을 무시하므로, 담당자가 쓴 줄바꿈이 보이게 줄 끝에 공백 두 칸(강제 줄바꿈)을 붙인다."""
    return re.sub(r"[ \t]*\r?\n", "  \n", (text or "").strip())


def _result(r: Recommendation, key: str, result: dict, checked: frozenset[int]) -> None:
    rule = amount_rules.find(r.service_id)
    ov = answer_override.find(r.service_id)
    has_ov_amounts = bool(ov and ov.get("amounts"))
    groups = benefit_tiers.evaluate(result["tiers"], set(checked))
    got = groups["base"] + groups["met"]
    st.markdown("##### ✅ 체크한 조건으로 받을 수 있는 지원")
    custom = (ov or {}).get("support_lines")
    if custom:                                    # 담당자가 직접 쓴 지원 내용 줄이 LLM이 정리한 항목을 대신한다
        shown = [l for l in custom if not l.get("when") or set(l["when"]) & checked]      # 표시 조건이 있는 줄은 그 체크 항목 중 하나라도 체크했을 때만
        for line in shown:
            st.markdown(f"**{line['label']}** — {_break_lines(line['text'])}")
        if not shown:
            st.info("체크한 조건으로 받을 수 있는 지원이 아직 없어요. 조건을 바꿔 다시 '체크 완료'를 눌러 보세요.")
        groups = {**groups, "pending": []}
    elif got:
        for t in got:
            _tier_line(t, show_amount=rule is None and not has_ov_amounts)
    else:
        st.info("체크한 조건으로 받을 수 있는 지원이 아직 없어요. 조건을 바꿔 다시 '체크 완료'를 눌러 보세요.")
    if groups["pending"]:
        with st.expander(f"조건을 더 충족하면 받을 수 있는 지원 {len(groups['pending'])}가지", expanded=not got):
            for t in groups["pending"]:
                _tier_line(t, show_amount=rule is None and not has_ov_amounts)
                st.caption(f"필요한 조건: {_needs_text(r, t['requires'])}  (아직 체크 안 함: {_needs_text(r, t['missing'])})")
    _override_amounts(ov)
    if rule:
        _amount_form(r, rule, key, checked)
    ec_rule = earned_credit_rules.find(r.service_id)
    if ec_rule:
        _earned_credit_form(r, ec_rule, key)
    if result["notes"]:
        st.caption("참고 (자료 밖 일반 정보라 정확하지 않을 수 있어요): " + " / ".join(n["text"] for n in result["notes"]))
    st.caption("체크는 스스로 확인한 결과예요. 금액은 소득·가구 등에 따라 달라질 수 있으니 최종 내용은 담당 기관에서 확인해 주세요.")


def render(r: Recommendation, key: str) -> None:
    """체크리스트 아래 '내 조건으로 받을 수 있는 지원 보기'. 체크할 조건이 있으면 '체크 완료' 후에만 나온다."""
    if not r.service_id:
        return
    n = len(r.needs_check)
    open_key = f"tiers_open_{key}"
    checked = _checked(key, n)
    if n:
        done = st.session_state.get(f"checks_done_{key}")
        if done is None:
            return
        if done != checked:
            st.session_state[open_key] = False
            st.caption("체크를 바꿨어요. 다시 '체크 완료'를 눌러 주세요.")
            return
    cache = st.session_state.setdefault("tiers_cache", {})
    with st.container(key=f"card_tiers_{key}"):
        if not st.session_state.get(open_key):
            if not st.button("🎁 내 조건으로 받을 수 있는 지원 보기", key=f"tiers_btn_{key}"):
                return
            st.session_state[open_key] = True
        ckey = (r.service_id, tuple(r.needs_check))
        if (answer_override.find(r.service_id) or {}).get("support_lines"):
            result = {"tiers": [], "notes": []}      # 담당자가 지원 내용 줄을 직접 썼으면 LLM 정리가 필요 없다 (LLM 실패·한도 초과와 무관하게 나온다)
        else:
            if ckey not in cache:
                with st.spinner("조건별 지원과 금액을 정리하는 중…"):
                    computed = benefit_tiers.compute(r.service_id, r.service_name, list(r.needs_check))
                if computed is None:
                    if _override_amounts(answer_override.find(r.service_id)):       # 담당자가 쓴 금액 안내는 정리가 실패해도 보여 준다
                        return
                    ec_rule = earned_credit_rules.find(r.service_id)
                    if ec_rule:                                                     # 코드로 계산하는 장려금 계산기도 LLM 정리와 상관없이 보여 준다
                        _earned_credit_form(r, ec_rule, key)
                        return
                    st.session_state[open_key] = False
                    st.warning("조건별 지원을 정리하지 못했어요. 카드의 원문 확인하기 링크로 확인해 주세요.")
                    return
                cache[ckey] = computed
            result = cache[ckey]
        _result(r, key, result, checked)
        if st.button("닫기", key=f"tiers_close_{key}"):
            st.session_state[open_key] = False
            st.rerun()
