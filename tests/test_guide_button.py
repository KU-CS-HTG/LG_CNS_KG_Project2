"""판정 기준 문서 버튼: 연결된 문서가 색인에 있는 제도에만 보인다 (guide_evidence.has_guides).
가짜 색인 조각 파일로 확인한다 (LLM·임베딩 모델 없음)."""
import json
import sys
import types

import pytest

from src.common.schema import Answer, Recommendation
from src.generate import guide_evidence


def _chunk(cid, service_ids):
    return {"chunk_id": cid, "doc_id": "d", "doc_title": "문서", "publisher": "기관", "year": 2026, "page": 1,
            "heading": "", "text": "본문", "scope": "전국", "service_ids": service_ids, "source_url": ""}


@pytest.fixture
def fake_index(tmp_path, monkeypatch):
    """chunks.jsonl을 가진 가짜 판정 문서 검색 모듈. write(조각들)로 색인 내용을 바꾼다."""
    from src.ingest import guides

    module = types.ModuleType("src.index.guide_store")
    module.INDEX_DIR = tmp_path
    module.guides = guides
    module.search_guides = lambda *a, **k: []
    monkeypatch.setitem(sys.modules, "src.index.guide_store", module)
    monkeypatch.setattr(guide_evidence, "_linked", {})

    def write(chunks, mtime=None):
        path = tmp_path / "chunks.jsonl"
        path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in chunks), encoding="utf-8")
        if mtime is not None:
            import os
            os.utime(path, (mtime, mtime))
    return write


def test_only_linked_services_have_guides(fake_index):
    fake_index([_chunk("a", ["WII000001410", "336000000096"]), _chunk("b", ["149200005007"])])
    assert guide_evidence.has_guides("WII000001410")
    assert guide_evidence.has_guides("149200005007")
    assert not guide_evidence.has_guides("SVC_NO_DOC")
    assert not guide_evidence.has_guides("")


def test_common_document_covers_every_service(fake_index):
    fake_index([_chunk("a", ["WII000001410"]), _chunk("common", [])])
    assert guide_evidence.has_guides("SVC_NO_DOC")


def test_rebuilt_index_is_read_again(fake_index):
    fake_index([_chunk("a", ["WII000001410"])], mtime=1_000_000)
    assert not guide_evidence.has_guides("149200005007")
    fake_index([_chunk("a", ["WII000001410"]), _chunk("b", ["149200005007"])], mtime=2_000_000)
    assert guide_evidence.has_guides("149200005007")


def test_missing_index_hides_button(tmp_path, monkeypatch):
    from src.ingest import guides
    module = types.ModuleType("src.index.guide_store")
    module.INDEX_DIR, module.guides = tmp_path / "없음", guides
    monkeypatch.setitem(sys.modules, "src.index.guide_store", module)
    monkeypatch.setattr(guide_evidence, "_linked", {})
    assert not guide_evidence.has_guides("WII000001410")


def test_guide_targets_keep_only_linked_services(fake_index):
    from app import main as app
    fake_index([_chunk("a", ["WII000001410"])])
    recs = [Recommendation(service_name="생계급여", agency="보건복지부", reason="", benefit="", how_to_apply="",
                           service_id="WII000001410"),
            Recommendation(service_name="청년월세 지원", agency="국토교통부", reason="", benefit="", how_to_apply="",
                           service_id="SVC_NO_DOC")]
    thread = [{"role": "user", "content": "질문"}, {"role": "assistant", "content": Answer(text="", recommendations=recs)}]
    assert [r.service_id for r in app._guide_targets(thread)] == ["WII000001410"]


def _cards_script():
    import streamlit as st

    from app import main as app
    from src.common.schema import Answer, Recommendation

    app._search_ready = lambda: False
    if "threads" not in st.session_state:
        need = ["가구 — 동일한 주민등록표상에 등재된 사람"]
        recs = [Recommendation(service_name="생계급여", agency="보건복지부", reason="이유", benefit="생계비",
                               how_to_apply="방문", needs_check=need, service_id="WII000001410"),
                Recommendation(service_name="청년월세 지원", agency="국토교통부", reason="이유", benefit="월세",
                               how_to_apply="방문", needs_check=need, service_id="SVC_NO_DOC")]
        st.session_state.threads = [[{"role": "user", "content": "생활비가 부족해요", "follow": False},
                                     {"role": "assistant", "content": Answer(text="", recommendations=recs)}]]
        st.session_state.current = 0
    app.main()


def test_card_button_only_on_linked_service(fake_index):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    fake_index([_chunk("a", ["WII000001410"])])
    at = AppTest.from_function(_cards_script).run(timeout=30)
    keys = [b.key for b in at.button if "판정 기준 문서에서 확인하기" in b.label]
    assert len(keys) == 1
    assert any("판정 기준으로 물어보기" in b.label for b in at.button)
