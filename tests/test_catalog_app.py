"""제도 목록 관리 화면(app/catalog_mode.py): 가짜 제도와 임시 폴더로 수정·숨기기 흐름만 확인한다."""
import pytest

pytest.importorskip("streamlit", reason="pip install -r requirements.txt 필요")
from streamlit.testing.v1 import AppTest  # noqa: E402

from src.catalog.store import CatalogStore  # noqa: E402

API = {"S1": {"service_id": "S1", "service_name": "청년 월세 지원", "agency": "국토교통부", "field": "주거·자립", "support_target": "청년",
              "selection_criteria": "", "support_content": "월 20만원", "how_to_apply": "온라인", "application_period": "상시신청",
              "deadline": None, "detail_url": "https://www.gov.kr/x", "eligibility": {}}}


def test_edit_hide_and_add_through_the_page(tmp_path, monkeypatch):
    import app.catalog_mode as cm

    root = tmp_path / "catalog"
    monkeypatch.setattr(cm, "CatalogStore", lambda: CatalogStore(root))
    monkeypatch.setattr(cm, "_api", lambda: API)
    script = tmp_path / "wrap.py"
    script.write_text("import app.catalog_mode as r\nr.render()\n", encoding="utf-8")
    at = AppTest.from_file(str(script), default_timeout=30).run()
    at.session_state["catalog_sel_id"] = "S1"                                                  # 표에서 S1을 클릭한 것과 같은 상태
    at.run()
    assert not at.exception and [b for b in at.button if b.label == "저장"][0].disabled        # 이름 없으면 쓸 수 없다
    at.sidebar.text_input[0].set_value("kim").run()
    next(t for t in at.text_area if t.key == "e:S1:support_content").set_value("월 30만원")
    next(b for b in at.button if b.label == "저장").click().run()
    assert not at.exception and CatalogStore(root).overlay().edits["S1"] == {"support_content": "월 30만원"}
    next(b for b in at.button if b.label == "답변에서 숨기기").click().run()
    assert CatalogStore(root).overlay().hidden == {"S1"}
    next(t for t in at.text_input if t.key == "add:service_name").set_value("K-패스")
    next(t for t in at.text_area if t.key == "add:support_content").set_value("대중교통비 환급")
    next(b for b in at.button if b.label == "제도 추가").click().run()
    assert any(s.startswith("MAN-") for s in CatalogStore(root).overlay().manual)


def test_pick_selected_maps_row_to_service_and_keeps_previous_when_cleared():
    import app.catalog_mode as cm

    view = [{"service_id": "A"}, {"service_id": "B"}]
    assert cm.pick_selected([1], view, None) == "B"
    assert cm.pick_selected([], view, "A") == "A"                  # 저장으로 표가 다시 그려져 선택이 풀려도 유지
    assert cm.pick_selected([5], view, "A") == "A"                 # 범위 밖이면 무시
    assert cm.pick_selected([], [], None) is None


def test_detail_opens_for_the_clicked_service_and_edits_it(tmp_path, monkeypatch):
    import app.catalog_mode as cm

    root = tmp_path / "catalog"
    api = {**API, "S2": {**API["S1"], "service_id": "S2", "service_name": "다른 제도", "support_content": "원래 내용"}}
    monkeypatch.setattr(cm, "CatalogStore", lambda: CatalogStore(root))
    monkeypatch.setattr(cm, "_api", lambda: api)
    script = tmp_path / "wrap2.py"
    script.write_text("import app.catalog_mode as r\nr.render()\n", encoding="utf-8")
    at = AppTest.from_file(str(script), default_timeout=30).run()
    assert not at.exception and any("클릭" in i.value for i in at.info)       # 고르기 전에는 안내만
    at.session_state["catalog_sel_id"] = "S2"                                 # 표에서 S2를 클릭한 것과 같은 상태
    at.run()
    assert any(t.key == "e:S2:support_content" and t.value == "원래 내용" for t in at.text_area)
    at.sidebar.text_input[0].set_value("kim").run()
    next(t for t in at.text_area if t.key == "e:S2:support_content").set_value("바뀐 내용")
    next(b for b in at.button if b.label == "저장").click().run()
    assert CatalogStore(root).overlay().edits["S2"] == {"support_content": "바뀐 내용"}
    assert at.session_state["catalog_sel_id"] == "S2"                         # 저장 뒤에도 같은 제도가 열려 있다
    next(b for b in at.button if b.label == "선택 닫기").click().run()
    assert at.session_state["catalog_sel_id"] is None
