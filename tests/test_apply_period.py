"""W3(실제 신청 링크)·W4(설명형 신청기한 자세히 확인) (docs/WORK_ITEMS.md)."""
import json

import pytest

from src.common.schema import Chunk, Citation, Recommendation
from src.generate import apply_link, period_detail
from src.ingest import apply_links


def _chunks(sid: str, how: str = "", name: str = "제도") -> list[Chunk]:
    return [Chunk(chunk_id=f"{sid}#신청방법", service_id=sid, service_name=name, section="신청방법", text=f"{name}\n{how}")]


@pytest.fixture
def links(monkeypatch):
    """원본 대응표와 A 저장소를 가짜로 바꾼다 (실제 data/raw·services.db를 읽지 않음)."""
    table: dict[str, dict] = {}
    monkeypatch.setattr(apply_links, "_cache", table)
    monkeypatch.setattr(apply_link, "_stored_how", lambda sid: "")
    return table


# ---- W3: 신청 링크 ----

def test_verified_program_page_comes_first(links):
    # 국민취업지원제도: 원본 필드는 고용24 첫 화면이라 사이트 안에서 다시 찾아야 했다 -> 확인한 제도 페이지로
    links["149200005007"] = {"url": "https://www.work24.go.kr", "org": "전국 고용센터"}
    url = apply_link.find(_chunks("149200005007", "고용24(www.work24.go.kr)"), "")
    assert url == "https://www.work24.go.kr/ua/z/z/1300/selectEmssRqutIntro.do" and apply_link.kind(url) == "page"
    assert apply_link.find(_chunks("149200005032"), "") == apply_link.VERIFIED_PAGES["149200005032"]   # K-디지털
    for u in apply_link.VERIFIED_PAGES.values():                                # 표의 주소는 모두 정상 주소
        assert apply_link.normalize(u) == u


def test_official_field_url_before_how_text(links):
    # 원본 '온라인신청사이트URL'이 신청방법 원문의 주소보다 먼저
    links["A1"] = {"url": "https://www.kosaf.go.kr", "org": ""}
    url = apply_link.find(_chunks("A1", "복지로(www.bokjiro.go.kr) 신청"), "")
    assert url == "https://www.kosaf.go.kr" and apply_link.kind(url) == "home"


def test_work24_home_becomes_program_search(links):
    # 표에 없는 고용24 제도는 첫 화면 대신 제도 이름 검색 결과로 (검색하면 신청 메뉴가 바로 나온다)
    how = "고용센터 방문 신청 또는 고용24(www.work24.go.kr) 홈페이지 신청"
    url = apply_link.find(_chunks("B1", how, name="청년일경험지원"), "")
    assert url == apply_link.WORK24_SEARCH + "%EC%B2%AD%EB%85%84%EC%9D%BC%EA%B2%BD%ED%97%98%EC%A7%80%EC%9B%90"
    assert apply_link.kind(url) == "search"
    links["B2"] = {"url": "https://www.work24.go.kr/wk/a/b/1200/x.do", "org": ""}     # 첫 화면이 아니면 그대로
    assert apply_link.find(_chunks("B2", name="x"), "") == "https://www.work24.go.kr/wk/a/b/1200/x.do"


def test_url_written_in_how_to_apply_text(links):
    # 원본 필드가 비어 있고 신청방법 원문에만 주소가 있다
    how = "한국장학재단 홈페이지(www.kosaf.go.kr/ko/main.do) 신청"
    assert apply_link.find(_chunks("C1", how), "") == "https://www.kosaf.go.kr/ko/main.do"
    assert apply_link.find(_chunks("X", ""), "온라인 신청: https://apply.jobaba.net/main.do 접속") \
        == "https://apply.jobaba.net/main.do"                                # 카드의 신청방법 글에서도 찾는다


def test_stored_how_to_apply_is_last_resort(monkeypatch, links):
    monkeypatch.setattr(apply_link, "_stored_how", lambda sid: "한국장학재단 홈페이지(www.kosaf.go.kr) 신청")
    assert apply_link.find(_chunks("Y", "방문 신청"), "방문 신청") == "https://www.kosaf.go.kr"


def test_no_link_is_not_invented(links):
    # 전화번호·이메일·'홈페이지'라는 말만 있으면 링크가 없다
    how = "주민센터 방문 신청, 문의 1544-9944, help@korea.kr, 구청 홈페이지 참조"
    assert apply_link.find(_chunks("Z", how), how) == ""
    assert apply_link.find([], "www.work24.go.kr") == ""


def test_normalize_urls():
    n = apply_link.normalize
    assert n("www.work24.go.kr") == "https://www.work24.go.kr"
    assert n("gb.go.kr") == "https://gb.go.kr"                               # 원본 필드에 http 없이 쓴 값
    assert n("https://ipsi.kopo.ac.kr/") == "https://ipsi.kopo.ac.kr/"
    assert n("http://www.kopo.ac.kr") == "http://www.kopo.ac.kr"
    assert n("http://상생페이백.kr") == "http://상생페이백.kr"                 # 원본 필드의 한글 도메인
    assert n("www.work24.go.kr).") == "https://www.work24.go.kr"            # 문장 끝 기호
    for bad in ("", "홈페이지", "1544-9944", "help@korea.kr", "www.work24 .go.kr"):
        assert n(bad) == ""


def test_build_link_table_from_raw(tmp_path):
    run = tmp_path / "20261020T005030Z-abc"
    run.mkdir()
    rows = [{"서비스ID": "A1", "온라인신청사이트URL": "https://www.work24.go.kr", "접수기관명": "전국 고용센터"},
            {"서비스ID": "A2", "온라인신청사이트URL": "", "접수기관명": ""},             # 둘 다 비면 넣지 않는다
            {"서비스ID": "A3", "온라인신청사이트URL": "", "접수기관명": "한국폴리텍대학"}]
    (run / "serviceDetail-00001.json").write_text(json.dumps({"data": rows}, ensure_ascii=False), encoding="utf-8")
    (run / "serviceDetail-summary.json").write_text("{}", encoding="utf-8")            # 요약 파일은 건너뛴다
    assert apply_links.latest_run(tmp_path) == run
    table = apply_links.build(run)
    assert table == {"A1": {"url": "https://www.work24.go.kr", "org": "전국 고용센터"},
                     "A3": {"url": "", "org": "한국폴리텍대학"}}
    apply_links.save(table, tmp_path / "links.json")
    monkey = apply_links._cache
    try:
        apply_links._cache = None
        assert apply_links.load(tmp_path / "links.json") == table
        apply_links._cache = None
        assert apply_links.load(tmp_path / "없음.json") == {}                         # 파일이 없으면 빈 표
    finally:
        apply_links._cache = monkey


def test_apply_button_html():
    from app import card_apply
    r = Recommendation(service_name="국민취업지원제도", agency="고용노동부", reason="", benefit="", how_to_apply="",
                       apply_url=apply_link.VERIFIED_PAGES["149200005007"])
    html = card_apply.apply_html(r)
    assert f'href="{r.apply_url}"' in html and card_apply.BUTTON["page"] in html and "www.work24.go.kr" in html
    r.apply_url = "https://www.kosaf.go.kr"                                    # 첫 화면은 '바로가기'라고 하지 않는다
    assert card_apply.BUTTON["home"] in card_apply.apply_html(r) and card_apply.NOTE["home"] in card_apply.apply_html(r)
    r.apply_url = apply_link.WORK24_SEARCH + "x"
    assert card_apply.BUTTON["search"] in card_apply.apply_html(r)
    r.apply_url = ""
    assert card_apply.NOT_FOUND_MESSAGE in card_apply.apply_html(r) and "href" not in card_apply.apply_html(r)
    r.apply_url = "javascript:alert(1)"                                        # http(s)가 아니면 링크로 쓰지 않는다
    assert "href" not in card_apply.apply_html(r)


def test_next_step_and_before_on_verified_pages():
    # 신청 양식은 로그인 뒤 스크립트로 열려 주소가 없다 -> 페이지에서 누를 버튼과 먼저 할 일을 알려 준다
    for u in list(apply_link.NEXT_STEP) + list(apply_link.BEFORE):
        assert u in apply_link.VERIFIED_PAGES.values()                       # 확인한 페이지에만 붙인다
    from app import card_apply
    r = Recommendation(service_name="국민취업지원제도", agency="고용노동부", reason="", benefit="", how_to_apply="",
                       apply_url=apply_link.VERIFIED_PAGES["149200005007"])
    html = card_apply.apply_html(r)
    assert "취업지원신청" in html and "구직 등록 먼저 하기" in html
    assert html.index("resumeMngMain") < html.index(r.apply_url)            # 먼저 할 일이 앞에
    r.apply_url = "https://www.kosaf.go.kr"                                    # 모르는 페이지에는 붙이지 않는다
    assert "👉" not in card_apply.apply_html(r) and "①" not in card_apply.apply_html(r)


# ---- W4: 설명형 신청기한 ----

def test_descriptive_period_is_explained():
    assert period_detail.explain("각 과정(캠퍼스)별 모집기간 상이") == "각 과정(캠퍼스)별 모집기간 상이"   # 한국폴리텍
    assert period_detail.explain("접수기관 별 상이") == "접수기관 별 상이"
    assert period_detail.explain("추후 공지") == "추후 공지"
    assert period_detail.explain("○ 1차: 공고 확인\n○ 2차: 별도 안내") == "○ 1차: 공고 확인\n○ 2차: 별도 안내"  # 줄바꿈 유지


def test_period_with_dates_or_no_application_is_not_explained():
    for text in ("상시신청", "", "2026.1.19. ~ 2026.1.30.(추가모집 별도 진행 *문의 요망)",
                 "3월 5일 ~ 3월 31일 (상황에 따라 접수기간이 변동될 수 있음.)", "○ 정기신청 : 5.1.~5.31.",
                 "별도의 신청절차가 없음"):
        assert period_detail.explain(text) == "", text


def test_generator_marks_are_removed_before_judging():
    # generator가 신청기한 앞에 붙이는 표시는 원문이 아니다
    assert period_detail.explain("이번 신청 마감 · 다음 공고 확인 · 사업별 상이") == "사업별 상이"
    assert period_detail.explain("신청 예정 · 접수기관 별 상이") == "접수기관 별 상이"
    from src.generate import generator                                         # 문구가 바뀌면 여기서 알 수 있게
    assert period_detail._MARK_RE.match(f"{generator.UPCOMING_MARK} · x")
    assert period_detail._MARK_RE.match(f"{generator.CLOSED_RECURRING_MARK} · x")


def test_period_detail_html_has_original_text_and_agency_links():
    from app import card_period
    r = Recommendation(service_name="한국폴리텍", agency="한국폴리텍대학", reason="", benefit="", how_to_apply="",
                       apply_url="https://ipsi.kopo.ac.kr/", period_detail="각 과정(캠퍼스)별 모집기간 상이",
                       citations=[Citation(service_name="한국폴리텍", agency="한국폴리텍대학", quote="q",
                                           detail_url="https://www.gov.kr/portal/rcvfvrSvc/dtlEx/B55201400002")])
    html = card_period.period_html(r)
    assert "각 과정(캠퍼스)별 모집기간 상이" in html and card_period.GUIDE in html
    assert 'href="https://ipsi.kopo.ac.kr/"' in html and "gov.kr/portal" in html
    r.apply_url, r.citations = "", []
    html = card_period.period_html(r)
    assert "href" not in html and card_period.GUIDE in html                    # 링크가 없어도 안내는 보인다
