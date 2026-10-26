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
