"""제도 목록 관리: 이벤트 접기, 덧씌운 값, 색인 반영 계획, 숨김의 답변 반영."""
import pytest

from src.catalog import apply as catalog_apply
from src.catalog import effective
from src.catalog import store as cs
from src.catalog.store import CatalogError, CatalogStore

API = {"S1": {"service_id": "S1", "service_name": "청년 월세 지원", "agency": "국토교통부", "field": "주거·자립",
              "support_target": "만 19~34세 청년", "selection_criteria": "", "support_content": "월 20만원", "how_to_apply": "온라인",
              "application_period": "상시신청", "deadline": None, "detail_url": "https://www.gov.kr/x", "eligibility": {}},
       "S2": {"service_id": "S2", "service_name": "다른 제도 이름", "agency": "고용노동부", "field": "고용·창업",
              "support_target": "구직자", "selection_criteria": "", "support_content": "수당", "how_to_apply": "방문",
              "application_period": "상시신청", "deadline": None, "detail_url": "", "eligibility": {}}}


@pytest.fixture
def store(tmp_path):
    return CatalogStore(tmp_path / "catalog")


def test_add_validates_and_creates_manual_id(store):
    with pytest.raises(CatalogError):
        store.add_service("kim", {"service_name": ""})
    with pytest.raises(CatalogError):
        store.add_service("kim", {"service_name": "K-패스"})                       # 지원대상·지원내용 둘 다 없음
    with pytest.raises(CatalogError):
        store.add_service("kim", {"service_name": "K-패스", "support_content": "환급", "detail_url": "http://insecure"})
    with pytest.raises(CatalogError):
        store.add_service("kim", {"service_name": "K-패스", "support_content": "환급", "detail_url": "https://x.kr/?serviceKey=abc"})
    with pytest.raises(CatalogError):
        store.add_service("bad name!", {"service_name": "a", "support_content": "b"})
    sid = store.add_service("kim", {"service_name": "K-패스", "support_content": "대중교통비 환급", "detail_url": "https://korea-pass.kr/"})
    assert cs.is_manual(sid) and store.overlay().manual[sid]["service_name"] == "K-패스"


def test_edit_hide_unhide_revert_remove_follow_event_order(store):
    store.edit_service("kim", "S1", {"support_content": "월 30만원"})
    store.edit_service("lee", "S1", {"agency": "서울시"})
    store.hide("kim", "S2")
    o = store.overlay()
    assert o.edits["S1"] == {"support_content": "월 30만원", "agency": "서울시"} and o.is_hidden("S2")
    store.revert("kim", "S1", ["agency"])
    store.unhide("lee", "S2")
    o = store.overlay()
    assert o.edits["S1"] == {"support_content": "월 30만원"} and not o.is_hidden("S2")
    store.revert("kim", "S1")                                                   # 전부 되돌리기
    assert "S1" not in store.overlay().edits
    with pytest.raises(CatalogError):
        store.remove_manual("kim", "S1")                                        # API 제도는 숨기기만
    sid = store.add_service("kim", {"service_name": "새 제도", "support_target": "누구나"})
    store.remove_manual("kim", sid)
    o = store.overlay()
    assert sid not in o.manual and sid in o.removed and store.edit_service("kim", sid, {"agency": "x"}) is not None
    assert sid not in store.overlay().manual                                    # 삭제된 제도에 온 수정은 무시


def test_edit_with_no_change_is_not_recorded_and_unknown_field_rejected(store):
    assert store.edit_service("kim", "S1", {}) is None and store.overlay().events == []
    with pytest.raises(CatalogError):
        store.edit_service("kim", "S1", {"content_hash": "x"})
    with pytest.raises(CatalogError):
        store.edit_service("kim", "S1", {"service_name": "   "})


def test_effective_service_applies_edits_and_rederives_deadline(store):
    store.edit_service("kim", "S1", {"application_period": "2020.01.01~2020.01.31", "support_content": "월 30만원"})
    o = store.overlay()
    rec = effective.effective_service("S1", API["S1"], o)
    assert rec["support_content"] == "월 30만원" and rec["deadline"] == "2020-01-31" and rec["service_name"] == "청년 월세 지원"
    assert effective.effective_service("S2", API["S2"], o) is API["S2"]          # 안 고친 제도는 그대로
    assert API["S1"]["support_content"] == "월 20만원"                           # 원본 DB 레코드는 바뀌지 않는다


def test_manual_service_is_normalized_and_listed(store):
    sid = store.add_service("kim", {"service_name": "K-패스", "agency": "국토교통부", "support_content": "대중교통비 20% 환급",
                                    "application_period": "상시신청", "detail_url": "https://korea-pass.kr/"})
    o = store.overlay()
    rec = effective.effective_service(sid, None, o)
    assert rec["service_id"] == sid and rec["detail_url"] == "https://korea-pass.kr/" and rec["agency"] == "국토교통부"
    store.hide("kim", "S2")
    rows = {r["service_id"]: r for r in effective.listing(API, store.overlay())}
    assert rows[sid]["source"] == "직접 추가" and rows["S2"]["hidden"] and rows["S1"]["source"] == "API"


def test_apply_plan_and_index_calls(store, monkeypatch):
    from src.index import vector_store
    sid = store.add_service("kim", {"service_name": "K-패스", "support_content": "대중교통비 환급", "application_period": "상시신청"})
    store.edit_service("kim", "S1", {"support_content": "월 30만원"})
    store.hide("kim", "S2")
    o = store.overlay()
    p = catalog_apply.plan(API, o)
    assert p["remove"] == ["S2"] and sorted(p["upsert"]) == sorted(["S1", sid])
    calls = {}
    monkeypatch.setattr(vector_store, "remove_services", lambda ids: calls.setdefault("remove", ids))
    monkeypatch.setattr(vector_store, "build_index", lambda chunks: calls.setdefault("chunks", chunks))
    assert catalog_apply.apply(API, o, dry_run=True)["dry_run"] and not calls
    report = catalog_apply.apply(API, o)
    assert calls["remove"] == ["S2"] and {c.service_id for c in calls["chunks"]} == {"S1", sid}
    assert any("월 30만원" in c.text for c in calls["chunks"]) and report["chunks"] == len(calls["chunks"])
    store.unhide("kim", "S2")
    assert "S2" in catalog_apply.plan(API, store.overlay())["upsert"]            # 숨김을 풀면 원본 값으로 다시 색인


def test_hidden_ids_cache_follows_file_changes(store):
    assert cs.hidden_ids(store.root) == frozenset()
    store.hide("kim", "S2")
    assert cs.hidden_ids(store.root) == {"S2"}
    store.unhide("kim", "S2")
    assert cs.hidden_ids(store.root) == frozenset()


def test_generator_drops_hidden_services_even_before_index_apply(monkeypatch, fake_search, fake_conditions):
    from src.common.schema import Answer
    from src.generate import condition_extractor as ce
    from src.generate import generator

    case = fake_search("sr1")
    fake_conditions(case)
    first = case["results"][0].service_id
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {})
    monkeypatch.setattr(generator.catalog_store, "hidden_ids", lambda: frozenset({first}))
    result = type("R", (), {"chunks": list(case["results"]), "filters_used": {}, "relaxed": False})()
    monkeypatch.setattr(generator.hybrid, "search_with_info", lambda *a, **k: result)
    monkeypatch.setattr(generator, "_generate", lambda *a, **k: Answer(text="x"))
    monkeypatch.setattr(generator.evidence_mod, "select_evidence", lambda chunks, n: {c.service_id: [c] for c in chunks})
    _, selected = generator._answer_with_evidence(case["question"])
    assert first not in selected and selected


def test_corrupt_lines_are_skipped(store):
    store.hide("kim", "S2")
    (store.events_dir / "kim.jsonl").open("a", encoding="utf-8").write("{깨진 줄\n")
    store.edit_service("kim", "S1", {"agency": "x"})
    o = store.overlay()
    assert o.is_hidden("S2") and o.edits["S1"] == {"agency": "x"}
