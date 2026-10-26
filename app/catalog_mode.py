"""제도 목록 관리: 답변에 쓰이는 제도의 전체 목록을 보고, 추가·수정·숨김·삭제한다 (docs/CATALOG.md).

실행: python -m app.catalog_mode   (내부에서 `streamlit run`으로 다시 띄운다)
API 제도는 DB를 고치지 않고 덧씌운 변경(catalog/events/<이름>.jsonl)으로 관리한다. 숨김은 답변에서 바로 빠지고,
추가·수정은 '색인에 반영'을 눌러야 검색에 쓰인다. app/main.py와 독립된 화면이다."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from streamlit import runtime  # noqa: E402

from src.catalog import apply as catalog_apply  # noqa: E402
from src.catalog import effective  # noqa: E402
from src.catalog.store import EDITABLE, LABELS, CatalogError, CatalogStore, is_manual  # noqa: E402
from src.common import config  # noqa: E402
from src.generate.condition_extractor import FIELDS  # noqa: E402
from src.review.cli import load_services  # noqa: E402

SHOW_ROWS = 500          # 표에 그리는 최대 줄 수 (검색으로 좁히게 한다)
LONG = ("support_target", "selection_criteria", "support_content", "how_to_apply")


@st.cache_resource(show_spinner="제도 데이터를 읽는 중…")
def _api() -> dict[str, dict]:
    return load_services(config.DB_PATH)


def _field_input(key: str, value: str, prefix: str):
    label = LABELS[key]
    if key in LONG:
        return st.text_area(label, value=value, height=140, key=f"{prefix}:{key}")
    if key == "field":
        options = ["", *FIELDS] + ([value] if value and value not in FIELDS else [])
        return st.selectbox(label, options, index=options.index(value), key=f"{prefix}:{key}")
    return st.text_input(label, value=value, key=f"{prefix}:{key}")


def _detail(store: CatalogStore, editor: str, sid: str, api: dict, overlay) -> None:
    record = effective.effective_service(sid, api.get(sid), overlay)
    if record is None:
        st.info("삭제된 제도입니다.")
        return
    hidden = overlay.is_hidden(sid)
    st.markdown(f"**{record.get('service_name', '')}**  ·  `{sid}`  ·  " + ("직접 추가" if is_manual(sid) else "API 제도")
                + ("  ·  🚫 숨김 (답변에서 제외)" if hidden else ""))
    cols = st.columns(3)
    if cols[0].button("복구하기" if hidden else "답변에서 숨기기", key=f"hide:{sid}", disabled=not editor):
        (store.unhide if hidden else store.hide)(editor, sid)
        st.rerun()
    if is_manual(sid):
        sure = cols[1].checkbox("삭제 확인", key=f"sure:{sid}")
        if cols[2].button("삭제", key=f"del:{sid}", disabled=not (editor and sure)):
            store.remove_manual(editor, sid)
            st.rerun()
    with st.form(f"edit:{sid}"):
        values = {k: _field_input(k, record.get(k) or "", f"e:{sid}") for k in EDITABLE}
        note = st.text_input("수정 이유 (선택)", max_chars=200, key=f"enote:{sid}")
        if st.form_submit_button("저장", disabled=not editor):
            changed = {k: v for k, v in values.items() if (v or "").strip() != (record.get(k) or "").strip()}
            try:
                if not changed:
                    st.info("바뀐 내용이 없어요.")
                else:
                    store.edit_service(editor, sid, changed, note)
                    st.rerun()
            except CatalogError as exc:
                st.error(str(exc))
    if sid in overlay.edits:
        st.caption("원본(API)과 다르게 고친 항목: " + ", ".join(LABELS[k] for k in overlay.edits[sid]))
        keys = st.multiselect("원본 값으로 되돌릴 항목 (비우면 전부)", list(overlay.edits[sid]), format_func=LABELS.get, key=f"rv:{sid}")
        if st.button("원본 값으로 되돌리기", key=f"rvb:{sid}", disabled=not editor):
            store.revert(editor, sid, keys or None)
            st.rerun()
    with st.expander(f"수정 이력 {len(overlay.history.get(sid, []))}건"):
        for e in reversed(overlay.history.get(sid, [])):
            what = ", ".join(LABELS.get(k, k) for k in e["fields"]) if e["op"] in ("add", "edit") else ""
            st.write(f"{e['ts'][:19]}  {e['editor']}  {e['op']}  {what}  {e.get('note', '')}")


def _add_form(store: CatalogStore, editor: str) -> None:
    st.caption("API에 없는 제도를 직접 넣습니다. 서비스명과, 지원대상·지원내용 중 하나는 꼭 필요해요. 색인에 반영해야 검색에 쓰입니다.")
    with st.form("add_service", clear_on_submit=True):
        values = {k: _field_input(k, "", "add") for k in EDITABLE}
        note = st.text_input("추가한 이유·출처 (선택)", max_chars=200, key="add:note")
        if st.form_submit_button("제도 추가", disabled=not editor):
            try:
                sid = store.add_service(editor, values, note)
            except CatalogError as exc:
                st.error(str(exc))
            else:
                st.success(f"추가했어요: {sid}. '색인에 반영' 탭에서 반영하세요.")


def _apply_tab(api: dict, overlay) -> None:
    p = catalog_apply.plan(api, overlay)
    st.write(f"색인에서 지울 제도 {len(p['remove'])}개, 최신 값으로 다시 넣을 제도 {len(p['upsert'])}개"
             + (f", 건너뜀 {len(p['skipped'])}개" if p["skipped"] else ""))
    st.caption("숨김은 반영하기 전에도 답변에서 바로 빠집니다. 추가·수정은 임베딩 모델을 불러와 몇 분 걸릴 수 있어요. "
               "다시 눌러도 안전하고, A의 수집이 같은 제도를 다시 색인했다면 다시 반영하세요.")
    if st.button("색인에 반영", key="apply_index", disabled=not (p["remove"] or p["upsert"])):
        with st.spinner("색인에 반영하는 중…"):
            try:
                report = catalog_apply.apply(api, overlay)
            except Exception as exc:        # noqa: BLE001 — 색인·모델이 없을 때 원인을 보여 준다
                st.error(f"반영하지 못했어요: {exc!r}")
            else:
                st.success(f"반영했어요: 삭제 {report['remove']}개, 다시 넣음 {report['upsert']}개 (조각 {report['chunks']}개)")


def render() -> None:
    st.title("제도 목록 관리")
    store = CatalogStore()
    overlay = store.overlay()
    with st.sidebar:
        editor = st.text_input("편집자 이름 (영문·숫자)", value=st.session_state.get("catalog_editor", ""), placeholder="예: taegyu").strip()
        st.session_state["catalog_editor"] = editor
        st.caption(f"직접 추가 {len(overlay.manual)} · 수정 {len(overlay.edits)} · 숨김 {len(overlay.hidden)}")
        st.caption("변경은 catalog/events/<이름>.jsonl 에 쌓여요. git으로 올려 공유하세요.")
    if not editor:
        st.info("왼쪽에 편집자 이름을 입력하면 추가·수정·숨기기를 할 수 있어요. 이름 없이도 목록은 볼 수 있습니다.")
    try:
        api = _api()
    except (SystemExit, Exception) as exc:   # noqa: BLE001
        st.warning(f"API 제도 목록(DB)을 읽지 못했어요 ({exc}). 직접 추가한 제도만 보입니다.")
        api = {}
    rows = effective.listing(api, overlay)

    c1, c2, c3, c4 = st.columns([3, 1.4, 1.4, 1.4])
    text = c1.text_input("검색 (서비스명·소관기관·ID)", key="cat_q").strip().lower()
    source = c2.selectbox("출처", ["전체", "API", "직접 추가"], key="cat_src")
    status = c3.selectbox("상태", ["전체", "활성", "숨김"], key="cat_status")
    field = c4.selectbox("분야", ["전체", *FIELDS], key="cat_field")
    only_edited = st.checkbox("수정한 제도만", key="cat_edited")
    shown = [r for r in rows if (not text or text in f"{r['service_name']} {r['agency']} {r['service_id']}".lower())
             and source in ("전체", r["source"]) and (status == "전체" or r["hidden"] == (status == "숨김"))
             and (field == "전체" or r["field"] == field) and (not only_edited or r["edited"])]
    st.caption(f"전체 {len(rows):,}개 중 {len(shown):,}개" + (f" (앞 {SHOW_ROWS}개만 표시, 검색으로 좁혀 보세요)" if len(shown) > SHOW_ROWS else ""))
    view = shown[:SHOW_ROWS]
    st.dataframe([{"ID": r["service_id"], "서비스명": r["service_name"], "소관기관": r["agency"], "분야": r["field"],
                   "신청기한": r["application_period"], "출처": r["source"], "상태": "숨김" if r["hidden"] else "활성",
                   "수정": "✎" if r["edited"] else ""} for r in view], hide_index=True, height=260)

    tab_edit, tab_add, tab_apply = st.tabs(["제도 보기·수정", "새 제도 추가", "색인에 반영"])
    with tab_edit:
        if not view:
            st.info("조건에 맞는 제도가 없어요.")
        else:
            by_id = {r["service_id"]: r for r in view}
            sid = st.selectbox("제도 선택", list(by_id), key="catalog_sel",
                               format_func=lambda i: f"{'🚫 ' if by_id[i]['hidden'] else ''}{by_id[i]['service_name']}  ·  {by_id[i]['agency']}  ·  {i}")
            _detail(store, editor, sid, api, overlay)
    with tab_add:
        _add_form(store, editor)
    with tab_apply:
        _apply_tab(api, overlay)


def main() -> None:
    st.set_page_config(page_title="제도 목록 관리", page_icon="🗂️", layout="wide")
    render()


if __name__ == "__main__":
    if runtime.exists():
        main()
    else:
        from streamlit.web import cli as stcli
        sys.argv = ["streamlit", "run", str(Path(__file__).resolve())]
        sys.exit(stcli.main())
