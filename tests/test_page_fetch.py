"""공식 자료 페이지 읽기(page_fetch): 막히는 원인 구분, robots·요청 간격, 읽은 기록, 근거 구절 대조, 본문 붙여넣기."""
import io
import json

import pytest
import requests

from src.generate import page_fetch as pf

HTML = """<html><head><title>안내</title></head><body><nav>메뉴</nav><h1>제도 안내</h1>
<p>구직급여 수급자가 거주지에서 25km 이상 떨어진 곳에서 구직활동을 하면 교통비와 숙박비를 지급합니다.</p></body></html>"""


class _Raw:
    def __init__(self, body):
        self._b = io.BytesIO(body)

    def read(self, n=-1, decode_content=True):
        return self._b.read(n)


class Resp:
    def __init__(self, body=b"", status=200, ctype="text/html; charset=utf-8", text="", url="https://www.example.go.kr/a"):
        self.status_code, self.headers, self.url, self._text = status, {"Content-Type": ctype}, url, text
        self.raw = _Raw(body)
        self.encoding, self.apparent_encoding = "utf-8", "utf-8"

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def getter(routes):
    """주소 끝 -> 응답(또는 예외). robots.txt가 없으면 404."""
    def get(url, **kw):
        for key, val in routes.items():
            if url.endswith(key):
                if isinstance(val, Exception):
                    raise val
                return val
        return Resp(status=404)
    return get


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    pf._robots.clear()
    pf._last_request.clear()
    monkeypatch.setattr(pf, "check_url", lambda url: "" if url.startswith("http") else "주소는 https://로 시작해야 해요.")   # 테스트는 이름 풀이를 하지 않는다


def test_fetch_reads_html_and_records_the_snapshot(tmp_path):
    page = pf.fetch("https://www.example.go.kr/a", get=getter({"/a": Resp(HTML.encode())}), sleep=lambda s: None)
    assert page.title == "안내" and "25km" in page.text and "메뉴" in page.text and page.method == "requests" and page.status == 200
    path = pf.save_snapshot(page, tmp_path)
    again = pf.load_snapshot("https://www.example.go.kr/a", tmp_path)
    assert path.is_file() and again.text == page.text and pf.load_snapshot("https://없는.kr/x", tmp_path) is None


def test_failures_are_classified_by_cause_and_never_worked_around():
    url = "https://www.example.go.kr/a"
    cases = {
        "network": requests.ConnectionError("dns"),
        "network2": requests.Timeout("slow"),
        "blocked": Resp(status=403),
        "blocked2": Resp(status=429),
        "other": Resp(status=500),
    }
    causes = {}
    for name, val in cases.items():
        pf._robots.clear()
        with pytest.raises(pf.FetchError) as e:
            pf.fetch(url, get=getter({"/a": val}), sleep=lambda s: None)
        causes[name] = e.value.cause
        assert pf.HINTS[e.value.cause] in str(e.value)                                  # 원인과 해결 방법이 함께 나온다
    assert causes == {"network": "network", "network2": "network", "blocked": "blocked", "blocked2": "blocked", "other": "other"}


def test_captcha_login_and_empty_pages_stop_automation():
    def run(html):
        pf._robots.clear()
        with pytest.raises(pf.FetchError) as e:
            pf.fetch("https://www.example.go.kr/a", get=getter({"/a": Resp(html.encode())}), sleep=lambda s: None)
        return e.value.cause
    assert run("<html><body><p>보안문자를 입력해 주세요 captcha</p></body></html>") == "captcha"
    assert run("<html><body><p>로그인이 필요한 서비스입니다.</p></body></html>") == "login"
    assert run("<html><body><div id='app'></div><script>render()</script></body></html>") == "empty"


def test_robots_txt_is_respected_and_requests_are_spaced():
    robots = Resp(b"", ctype="text/plain", text="User-agent: *\nDisallow: /private/\n")
    get = getter({"/robots.txt": robots, "/private/x": Resp(HTML.encode()), "/ok": Resp(HTML.encode())})
    with pytest.raises(pf.FetchError) as e:
        pf.fetch("https://www.example.go.kr/private/x", get=get, sleep=lambda s: None)
    assert e.value.cause == "robots"
    sleeps = []
    clock = iter([100.0, 100.0, 100.5, 100.5, 103.0, 103.0])
    monkey_now = lambda: next(clock)
    pf._last_request.clear()
    pf._wait("h", sleeps.append, monkey_now)          # 첫 요청: 기다리지 않는다
    pf._wait("h", sleeps.append, monkey_now)          # 0.5초 뒤 두 번째: 1.5초 더 기다린다
    pf._wait("h", sleeps.append, monkey_now)          # 충분히 지난 뒤: 기다리지 않는다
    assert sleeps == [pytest.approx(1.5)]


def test_browser_mode_uses_the_given_renderer_and_explains_when_playwright_is_missing(monkeypatch):
    page = pf.fetch("https://www.example.go.kr/a", browser=True, get=getter({}), render=lambda url: HTML, sleep=lambda s: None)
    assert page.method == "browser" and "25km" in page.text
    pf._robots.clear()
    with pytest.raises(pf.FetchError) as e:                      # 이 환경에는 playwright가 없다 -> 설치 안내
        pf.fetch("https://www.example.go.kr/b", browser=True, get=getter({}), sleep=lambda s: None)
    assert e.value.cause == "other" and "playwright" in str(e.value).lower()


def test_pdf_pages_are_read(monkeypatch):
    monkeypatch.setattr(pf, "_pdf_text", lambda body: ("서식", "1년 이상 상환유예하는 경우에는 분할상환금을 12회차 이상 상환한 경우에만 적용됩니다."))
    page = pf.fetch("https://www.example.go.kr/form.pdf", get=getter({"/form.pdf": Resp(b"%PDF-1.4", ctype="application/pdf")}), sleep=lambda s: None)
    assert page.title == "서식" and "12회차" in page.text


def test_quote_check_ignores_spacing_but_not_wording():
    text = "재취업을 위해서 직업안정기관의 소개에 따라\n거주지로부터 25km가 넘는 지역에서 구직활동을 하는 경우"
    assert pf.quote_in_text(text, "소개에 따라 거주지로부터 25km가 넘는 지역에서")
    assert not pf.quote_in_text(text, "소개에 따라 거주지로부터 30km가 넘는 지역에서") and not pf.quote_in_text(text, "짧")


def test_check_hosts_reports_each_cause():
    get = getter({"a.kr/": Resp(status=200), "b.kr/": requests.ConnectionError("dns"), "c.kr/": Resp(status=403), "d.kr/": Resp(status=500)})
    rows = pf.check_hosts(("a.kr", "b.kr", "c.kr", "d.kr"), get=get, sleep=lambda s: None)
    assert [(h, s) for h, s, _ in rows] == [("a.kr", "ok"), ("b.kr", "network"), ("c.kr", "blocked"), ("d.kr", "other")]


def test_verify_overrides_compares_saved_quotes_with_page_text(tmp_path, monkeypatch):
    from src.generate import answer_override as ao
    monkeypatch.setattr(ao, "OVERRIDE_DIR", tmp_path / "ov")
    src = {"title": "t", "url": "https://www.example.go.kr/a", "level": "공식", "quote": "교통비와 숙박비를 지급합니다"}
    ao.save("S1", "claude", {"amounts": [{"label": "x", "text": "y"}], "source": src}, ai=True)
    ao.save("S2", "claude", {"amounts": [{"label": "x", "text": "y"}], "source": {**src, "url": "https://www.example.go.kr/b", "quote": "본문에 없는 문장입니다"}}, ai=True)
    ao.save("S3", "claude", {"amounts": [{"label": "x", "text": "y"}], "source": {**src, "url": "https://막힌.kr/c"}}, ai=True)
    ao.save("S4", "claude", {"benefit_text": "문구만"}, ai=True)

    def fetcher(url):
        if "막힌" in url:
            raise pf.FetchError("network")
        return pf.Page(url, url, "t", html_text, 200, "text/html", "2026-10-28T00:00:00+00:00", "requests", "h")
    html_text = pf.html_to_text(HTML)[1]
    snaps = tmp_path / "snaps"
    res = {r["service_id"]: r["status"] for r in pf.verify_overrides(fetcher=fetcher, folder=snaps)}
    assert res == {"S1": "ok", "S2": "missing", "S3": "unreachable", "S4": "no_source"}
    assert pf.load_snapshot("https://www.example.go.kr/a", snaps) is not None                  # 읽은 기록이 남는다
    def offline(url):
        assert "막힌" in url, "이미 읽은 주소는 기록을 써야 해요"
        raise pf.FetchError("network")
    again = {r["service_id"]: r["status"] for r in pf.verify_overrides(fetcher=offline, folder=snaps) if r["service_id"] in ("S1", "S2")}
    assert again == {"S1": "ok", "S2": "missing"}                                               # 이미 읽은 주소는 다시 접속하지 않는다


def test_fill_from_text_uses_pasted_body_and_verifies_against_it():
    from src.generate import amount_from_url as af
    service = {"service_id": "S1", "service_name": "광역구직활동비", "support_content": "교통비와 숙박료 지급"}
    body = "구직급여 수급자가 거주지에서 25km 이상 떨어진 곳에서 구직활동을 하면 교통비와 숙박비를 월 최대 10만원 지급합니다."
    ok = {"page_matches_service": True, "variants": [{"name": "광역구직활동비", "pay_type": "fixed", "amount_won": 100000, "months": 0,
                                                       "quote": "교통비와 숙박비를 월 최대 10만원 지급합니다"}], "note": ""}
    out = af.fill_from_text(service, "https://www.moel.go.kr/x", "안내", body, lambda s, u: ok)
    assert out["ok"] and out["form"]["variants"][0]["amount_won"] == 100000 and out["form"]["source"]["url"] == "https://www.moel.go.kr/x"
    fake = {**ok, "variants": [{**ok["variants"][0], "amount_won": 900000}]}
    assert not af.fill_from_text(service, "https://www.moel.go.kr/x", "안내", body, lambda s, u: fake)["ok"]      # 본문에 없는 금액은 버린다
    assert "주소" in af.fill_from_text(service, "", "안내", body, lambda s, u: ok)["message"]
    assert "짧" in af.fill_from_text(service, "https://x.kr", "안내", "짧은 글", lambda s, u: ok)["message"]


def test_page_paste_flow_fills_the_form_and_records_a_snapshot(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    import app.amount_mode as page
    from src.generate import amount_manual as am, amount_rules as ar, answer_override as ao

    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": []}), encoding="utf-8")
    monkeypatch.setattr(ar, "RULES_PATH", rules)
    monkeypatch.setattr(am, "REVIEWED_DIR", tmp_path / "reviewed")
    monkeypatch.setattr(ao, "OVERRIDE_DIR", tmp_path / "overrides")
    monkeypatch.setattr(pf, "SNAPSHOT_DIR", tmp_path / "snaps")
    svc = {"service_id": "S50", "service_name": "가상 생활안정 지원", "agency": "고용노동부", "field": "고용", "support_target": "청년", "selection_criteria": "",
           "support_content": "취업을 위한 생활안정 지원금을 지급합니다.", "how_to_apply": "고용센터 방문", "application_period": "상시", "detail_url": "https://www.gov.kr/x"}
    monkeypatch.setattr(page, "_services", lambda: {"S50": svc})
    monkeypatch.setattr(page, "_demand", lambda: {})
    body = "구직활동 지원금은 월 50만원씩 6개월간 지급한다. 만 19세부터 34세까지 신청할 수 있다."
    fill = {"page_matches_service": True, "variants": [{"name": "지원금", "pay_type": "fixed", "amount_won": 500000, "months": 6, "age_min": 19, "age_max": 34,
                                                         "quote": "월 50만원씩 6개월간 지급한다"}], "note": ""}
    monkeypatch.setattr("src.generate.condition_extractor._chat_json", lambda s, u: fill)
    script = tmp_path / "wrap.py"
    script.write_text("import app.amount_mode as r\nr.render()\n", encoding="utf-8")
    at = AppTest.from_file(str(script), default_timeout=30).run()
    at.session_state["amount_sel_id"] = "S50"
    at.run()
    assert next(b for b in at.button if b.label == "붙여넣은 본문으로 채우기").disabled                 # 주소·본문이 없으면 꺼져 있다
    at.text_input(key="am_S50_aiurl").set_value("https://www.moel.go.kr/x")
    at.text_input(key="am_S50_aipt").set_value("지원 안내")
    at.text_area(key="am_S50_aip").set_value(body).run()
    next(b for b in at.button if b.label == "붙여넣은 본문으로 채우기").click().run()
    assert not at.exception and any("AI가 자료에서 채운 것" in i.value for i in at.info)
    assert at.text_input(key="am_S50_a1_label").value == "지원금" and at.text_input(key="am_S50_surl").value == "https://www.moel.go.kr/x"
    assert at.text_area(key="am_S50_quote").value == "월 50만원씩 6개월간 지급한다"
    snap = pf.load_snapshot("https://www.moel.go.kr/x", tmp_path / "snaps")
    assert snap and snap.method == "pasted" and pf.quote_in_text(snap.text, "월 50만원씩 6개월간 지급한다")      # 나중에 verify로 대조할 수 있다
