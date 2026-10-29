"""W2: 내 조건으로 받을 수 있는 지원 (카드의 자격 체크리스트 아래).

흐름: 사용자가 체크리스트를 체크하고 '체크 완료'를 누르면 `mark_done`이 그 시점의 체크 상태를 기억한다 -> 체크가 그대로면
'내 조건으로 받을 수 있는 지원 보기' 버튼이 나온다 -> 누르면 제도의 조건별 지원(`benefit_tiers`, LLM 한 번)을 정리해 체크한
조건에 맞는 지원과 금액을 보여 준다. 금액 근거(원문 구절)는 '자세히 보기'에서 확인한다.
금액 규칙이 있는 제도(`amount_rules`)는 입력값으로 예상 수령액을 계산해 보여 준다.
체크를 바꾸면 결과를 닫고 다시 '체크 완료'를 누르게 한다. 체크할 때마다 LLM을 부르지 않는다.
지원내용 원문은 카드 머리 '무엇을 받나요' 칸의 '원문 확인하기' 링크(새 창)로 본다 (app/main.py)."""
from __future__ import annotations

from copy import deepcopy
import re

import streamlit as st

from src.common.schema import Recommendation
from src.generate import amount_rules, answer_override, benefit_tiers, benefit_applicability as applicability


def _invalidate_category(key: str) -> None:
    st.session_state[f"category_done_{key}"] = None
    st.session_state[f"category_open_{key}"] = False


def _category_state(key: str):
    ctx = st.session_state[f"category_context_{key}"]
    selection = {g: st.session_state.get(w) for g, w in ctx["selection_keys"].items()}
    checked = {rid for rid, w in ctx["requirement_keys"].items() if st.session_state.get(w)}
    values = {iid: st.session_state.get(w) for iid, w in ctx["input_keys"].items()}
    return ctx, selection, checked, values


def _mark_category_done(key: str) -> None:
    ctx, selection, checked, values = _category_state(key)
    result = applicability.evaluate(ctx["config"], selection, checked)
    if result["errors"] or result["selection_required"]:
        _invalidate_category(key)
        return
    st.session_state[f"category_done_{key}"] = applicability.confirmation(ctx["config"], key, selection, checked, values)
    st.session_state[f"category_open_{key}"] = False


def render_checklist(r: Recommendation, key: str, category: dict) -> None:
    """Exclusive selection is not a requirement confirmation. Widgets use stable IDs."""
    c, errors = applicability.validate(category, r.service_id)
    if c is None:
        _invalidate_category(key)
        st.warning("유형 설정을 검수해야 해요. 개인화 안내는 보류하고 원문 확인하기를 이용해 주세요.")
        return
    rule = amount_rules.find(r.service_id)
    c["calculation_revision"] = applicability.fingerprint(rule)
    revision = applicability.fingerprint(c)[:20]
    prefix = f"category_{key}_{r.service_id}_{revision}"
    selection_keys = {}
    selection = {}
    for group in c["groups"]:
        wk = f"{prefix}_group_{group['id']}"
        selection_keys[group["id"]] = wk
        options = {o["id"]: o for o in group["options"]}
        selection[group["id"]] = st.radio(group["label"], [None] + list(options), index=0,
            format_func=lambda value, options=options: "아직 모름 / 미선택" if value is None else options[value]["label"],
            key=wk, on_change=_invalidate_category, args=(key,))
        chosen = options.get(selection[group["id"]])
        if chosen and chosen.get("text"):
            st.caption("선택 유형의 정의: " + chosen["text"])
    result = applicability.evaluate(c, selection)
    requirement_keys, input_keys = {}, {}
    ctx = {"config": c, "rule": deepcopy(rule), "selection_keys": selection_keys, "requirement_keys": requirement_keys, "input_keys": input_keys}
    st.session_state[f"category_context_{key}"] = ctx
    if result["selection_required"]:
        _invalidate_category(key)
        st.info("가구 유형을 선택해 주세요. 유형 선택은 소득·재산 등 자격 확인을 대신하지 않아요.")
        return
    st.markdown("**선택 유형의 자격 조건 확인**")
    for req in result["requirements"]:
        wk = f"{prefix}_requirement_{req['id']}"
        requirement_keys[req["id"]] = wk
        st.checkbox(req["text"], key=wk, on_change=_invalidate_category, args=(key,))
    checked = sum(bool(st.session_state.get(w)) for w in requirement_keys.values())
    total = len(requirement_keys)
    st.caption(f"현재 유형의 확인 조건 {total}개 중 {checked}개 확인 · 미체크는 미확인이에요.")
    if total:
        st.progress(checked / total)
    # Inputs are collected BEFORE confirmation, and isolated by selected option.
    eligible = result["common"] + result["selected"] + result["pending"]
    mapped = {i.get("variant_id") for i in eligible if i["kind"] == "calculation"}
    if rule and rule.get("calculator") == "generic":
        variants = [v for v in rule["variants"] if applicability.variant_id(v) in mapped]
        for spec in applicability.calculation_inputs(rule, variants):
            wk = f"{prefix}_{applicability.fingerprint(selection)[:10]}_input_{spec['id']}"
            input_keys[spec["id"]] = wk
            args = {"key": wk, "on_change": _invalidate_category, "args": (key,)}
            if spec["type"] == "bool":
                st.radio(spec["label"], [None, True, False], index=0,
                         format_func=lambda v: "미입력" if v is None else "예" if v else "아니요", **args)
            elif spec["type"] == "choice":
                st.selectbox(spec["label"], spec["options"], index=None, **args)
            else:
                st.number_input(spec["label"], value=None, min_value=spec.get("min"), max_value=spec.get("max"),
                                step=1 if spec["type"] == "int" else 0.1, **args)
    st.button("체크 완료", key=f"category_confirm_{key}", on_click=_mark_category_done, args=(key,))


def _category_result(r: Recommendation, key: str) -> None:
    if f"category_context_{key}" not in st.session_state:
        return
    ctx, selection, checked, values = _category_state(key)
    result = applicability.evaluate(ctx["config"], selection, checked)
    if result["errors"] or result["selection_required"]:
        return
    token = applicability.confirmation(ctx["config"], key, selection, checked, values)
    if st.session_state.get(f"category_done_{key}") != token:
        st.session_state[f"category_open_{key}"] = False
        st.caption("유형·조건·계산 입력을 확인한 뒤 다시 '체크 완료'를 눌러 주세요.")
        return
    if not st.session_state.get(f"category_open_{key}"):
        if not st.button("🎁 내 조건으로 받을 수 있는 지원 보기", key=f"category_show_{key}"):
            return
        st.session_state[f"category_open_{key}"] = True
    with st.container(key=f"category_result_{key}"):
        labels = [o["label"] for g in ctx["config"]["groups"] for o in g["options"] if selection[g["id"]] == o["id"]]
        st.markdown("##### " + " · ".join(labels) + " 기준 지원 안내")
        visible = result["common"] + result["selected"] + result["pending"]
        reqs = {i["id"]: i["text"] for i in result["requirements"]}
        for item in visible:
            if item["kind"] == "calculation":
                continue
            st.markdown(f"**{item.get('label') or '안내'}**")
            st.text(item["text"])
            if item["kind"] == "amount":
                st.caption("금액 기준 안내이며 개인 예상 수령액이 아니에요.")
            if item["missing"]:
                st.caption("남은 확인: " + " / ".join(reqs[i] for i in item["missing"]))
            _category_sources(item)
        for calc in applicability.calculation(ctx["rule"], ctx["config"], selection, checked, values):
            item = calc["item"]
            st.markdown(f"**{item.get('label') or '계산 안내'}**")
            if "output" not in calc:
                st.info("계산 전 확인 필요: " + " / ".join(calc.get("pending", []) + [reqs[i] for i in calc.get("requirements", [])]))
            else:
                for scenario in calc["output"]["scenarios"]:
                    if scenario["amount_won"] is not None:
                        st.markdown(f"예상 수령액: {amount_rules.won(scenario['amount_won'])}")
                    for text in scenario["lines"] + scenario["reasons"]:
                        st.text(text)
            _category_sources(item)
        ov = answer_override.find(r.service_id) or {}
        if result["unresolved"] or any(req["applicability"]["kind"] == "unresolved" for req in ctx["config"].get("requirements", [])) or any(ov.get(k) for k in ("support_lines", "amounts", "notes")):
            st.caption("기존 문구를 별도로 넘기지 않고 적용 유형을 확인한 항목만 표시해요. 미확인 항목은 보류하며 원문 확인하기에서 원자료를 볼 수 있어요.")
        if ctx["config"].get("warnings"):
            st.caption("원문 연결이 불충분한 항목은 표시하지 않아요. 유형별 안내는 사람의 자격 승인이나 개인 수령액 계산이 아니에요.")
        st.caption("선택 유형과 자가 확인에 따른 안내예요. 미확인은 부적격을 뜻하지 않으며 실제 자격·지급액은 담당 기관 심사로 정해져요.")


def _category_sources(item):
    if item.get("evidence"):
        with st.expander("근거 보기 — " + (item.get("label") or item["id"])):
            for source in item["evidence"]:
                st.text(source["quote"])
                st.markdown(f"[{source.get('title') or '출처'}]({source['url']})")


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
    if result["notes"]:
        st.caption("참고 (자료 밖 일반 정보라 정확하지 않을 수 있어요): " + " / ".join(n["text"] for n in result["notes"]))
    st.caption("체크는 스스로 확인한 결과예요. 금액은 소득·가구 등에 따라 달라질 수 있으니 최종 내용은 담당 기관에서 확인해 주세요.")


def render(r: Recommendation, key: str) -> None:
    """체크리스트 아래 '내 조건으로 받을 수 있는 지원 보기'. 체크할 조건이 있으면 '체크 완료' 후에만 나온다."""
    if not r.service_id:
        return
    category = applicability.config_for(r.service_id, answer_override.find(r.service_id))
    if category is not None:
        # The grouped path never falls back to unclassified generated/manual text.
        if "invalid" not in category:
            current = {**category, "calculation_revision": applicability.fingerprint(amount_rules.find(r.service_id))}
            prepared = st.session_state.get(f"category_context_{key}", {}).get("config")
            if current != prepared:
                _invalidate_category(key)
                st.caption("원문 또는 설정이 바뀌었어요. 유형과 조건을 다시 확인해 주세요.")
                return
            _category_result(r, key)
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
        ckey = ("tiers-v2", r.service_id, tuple(r.needs_check), applicability.fingerprint(
            [r.benefit_detail, r.benefit, answer_override.find(r.service_id), amount_rules.find(r.service_id)]))
        if (answer_override.find(r.service_id) or {}).get("support_lines"):
            result = {"tiers": [], "notes": []}  # Reviewed legacy lines do not need LLM preparation.
        else:
            if ckey not in cache:
                with st.spinner("조건별 지원과 금액을 정리하는 중…"):
                    computed = benefit_tiers.compute(r.service_id, r.service_name, list(r.needs_check))
                if computed is None:
                    if _override_amounts(answer_override.find(r.service_id)):
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
