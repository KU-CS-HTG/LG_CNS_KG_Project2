"""Explicit applicability editor. Editing/previewing is not publication."""
from copy import deepcopy

import streamlit as st

from src.generate import benefit_applicability as ap


def _initial(key, value):
    return {} if key in st.session_state else {"value": value}


def _scope(row, group, prefix):
    choices = {"적용 유형 검수 필요": "unresolved", "공통 적용": "common", "지정 유형": "options"}
    old = row.get("applicability", {}).get("kind", "unresolved")
    labels = list(choices)
    key = prefix + "_scope"
    kind = choices[st.selectbox("적용 범위", labels, key=key,
        **({} if key in st.session_state else {"index": next((i for i, l in enumerate(labels) if choices[l] == old), 0)}))]
    if kind != "options":
        return {"kind": kind}
    opts = {o["id"]: o["label"] for o in group["options"]}
    selected = st.multiselect("적용 유형 (복수 가능)", list(opts), format_func=opts.get, key=prefix + "_options",
        **({} if prefix + "_options" in st.session_state else {"default": [v for v in row.get("applicability", {}).get("option_ids", []) if v in opts]}))
    return {"kind": "options", "group_id": group["id"], "option_ids": selected}


def render(sid, service, saved, raw, rule=None):
    """Return an optional draft. Legacy entries enter as unresolved, not common."""
    seed = saved.get("applicability") or ap.config_for(sid, service=service)
    prefix = f"category_editor_{sid}"
    with st.expander("유형별 적용 설정 — 검수 후 저장", expanded=seed is not None):
        if not st.checkbox("이 제도에 상호 배타적인 유형 선택 사용", key=prefix + "_enabled", **_initial(prefix + "_enabled", seed is not None)):
            return None
        st.caption("유형 선택과 소득·재산 확인은 별개예요. 미분류 항목은 공통으로 처리하지 않아요. 저장/확인 완료 전에는 실제 답변에 반영되지 않아요.")
        seed = deepcopy(seed or {"schema_version": ap.VERSION, "program_id": sid, "groups": [], "requirements": [], "items": []})
        old_group = (seed.get("groups") or [{}])[0]
        gid = st.text_input("그룹 ID (표시 순서와 무관한 고정 ID)", key=prefix + "_gid", **_initial(prefix + "_gid", old_group.get("id", "household")))
        glabel = st.text_input("선택 질문", key=prefix + "_glabel", **_initial(prefix + "_glabel", old_group.get("label", "유형")))
        lines = st.text_area("선택 유형: 고정 ID | 표시 이름 (한 줄에 하나)", key=prefix + "_options_text",
            **_initial(prefix + "_options_text", "\n".join(o["id"] + " | " + o["label"] for o in old_group.get("options", []))))
        old_options = {o["id"]: o for o in old_group.get("options", [])}
        options = []
        for line in lines.splitlines():
            oid, sep, label = line.partition("|")
            if line.strip():
                options.append({**old_options.get(oid.strip(), {}), "id": oid.strip(), "label": label.strip() if sep else ""})
        group = {"id": gid, "label": glabel, "options": options}
        requirements, items = seed.get("requirements", []), seed.get("items", [])
        def append_missing(rows, text, kind, label=""):
            if text and not any(r.get("text") == text and (not kind or r.get("kind") == kind) for r in rows):
                rows.append({"id": (kind or "requirement") + "-" + ap.fingerprint(text)[:16], "text": text,
                             "applicability": {"kind": "unresolved"}, "evidence": [],
                             **({"kind": kind, "label": label, "requires": []} if kind else {})})
        for text in raw.get("needs_check", []):
            append_missing(requirements, text, None)
        for kind, collection in (("support", "support_lines"), ("amount", "amounts")):
            for item in raw.get(collection, []):
                text = item["text"] + ("\n조건: " + item["condition"] if item.get("condition") else "")
                append_missing(items, text, kind, item["label"])
        for text in raw.get("notes", []):
            append_missing(items, text, "note", "안내 메모")
        if rule and rule.get("calculator") == "generic":
            for note in ap.rule_notes(rule):
                append_missing(items, note, "note", "계산 규칙의 주의사항·예외")
            for variant in rule["variants"]:
                vid = ap.variant_id(variant)
                if not any(i.get("variant_id") == vid for i in items):
                    items.append({"id": "calculation-" + ap.fingerprint(vid)[:16], "kind": "calculation", "label": variant["name"],
                                  "text": "검수한 계산 규칙: " + variant["name"], "variant_id": vid, "requires": [],
                                  "applicability": {"kind": "unresolved"}, "evidence": []})
        for label, rows in (("자격 조건", requirements), ("지원·금액·메모·계산", items)):
            st.markdown("**" + label + " 적용 범위**")
            for row in rows:
                p = prefix + "_" + row["id"]
                st.caption("고정 ID: " + row["id"])
                row["text"] = st.text_area("조건/지원 내용", key=p + "_text", **_initial(p + "_text", row["text"]))
                if "kind" in row:
                    row["label"] = st.text_input("항목 제목", key=p + "_label", **_initial(p + "_label", row.get("label", "")))
                    ids = [r["id"] for r in requirements]
                    names = {r["id"]: r["text"] for r in requirements}
                    invalid = [i for i in row.get("requires", []) if i not in ids]
                    row["requires"] = st.multiselect("필요 확인 조건 ID", ids + invalid, format_func=lambda i: i + " — " + names.get(i, "잘못된 참조: 검수 필요"), key=p + "_requires",
                        **({} if p + "_requires" in st.session_state else {"default": row.get("requires", [])}))
                row["applicability"] = _scope(row, group, p)
                old = (row.get("evidence") or [{}])[0]
                title = st.text_input("적용 관계 근거 제목", key=p + "_etitle", **_initial(p + "_etitle", old.get("title", "")))
                url = st.text_input("적용 관계 근거 URL", key=p + "_eurl", **_initial(p + "_eurl", old.get("url", "")))
                quote = st.text_area("이 유형과 항목의 연결을 확인한 근거 구절", key=p + "_equote", **_initial(p + "_equote", old.get("quote", "")))
                row["evidence"] = [{"title": title, "url": url, "quote": quote}] if quote or url else []
        return {**seed, "groups": [group], "requirements": requirements, "items": items}
