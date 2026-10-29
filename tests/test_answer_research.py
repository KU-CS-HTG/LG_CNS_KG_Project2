"""AI 답변 채우기(answer_research): 가짜 검색·읽기·LLM으로 검증 규칙을 확인한다 (네트워크·키 불필요)."""
from datetime import date
from types import SimpleNamespace

import pytest

from src.generate import answer_override, answer_research, page_fetch

URL = "https://www.kamco.or.kr/portal/contents.do?mId=1"
BODY = "가계안정지원사업 안내. 채무자의 상환능력에 따라 원금의 20~70% 및 이자 전액 감면, 최장 10년간 분할상환. 한국자산관리공사 콜센터 1588-3570으로 신청."
SERVICE = {"service_id": "S1", "service_name": "국민행복기금 채무조정", "agency": "한국자산관리공사", "detail_url": "",
           "support_target": "연체채권 채무자", "support_content": "채무감면 및 분할상환", "how_to_apply": "상담 후 신청", "application_period": "상시"}


def fetch_ok(url):
    return page_fetch.page_from_text(url, "가계안정지원사업", BODY)


def llm(**over):
    base = {"relevant": [0, 1], "benefit_text": "원금과 이자를 감면하고 최장 10년까지 나눠 갚도록 도와요.", "how_to_apply": "① 콜센터 1588-3570에서 상담 후 신청해요.",
            "needs_check": ["국민행복기금 보유 연체채권의 채무자", "소득 요건 (확인 필요)"],
            "support_lines": [{"label": "분할상환", "text": "최장 10년간 나눠 갚아요.", "source": 1, "quote": "최장 10년간 분할상환"}],
            "amounts": [{"label": "원금 감면", "text": "원금의 20~70%", "condition": "", "source": 1, "quote": "원금의 20~70% 및 이자 전액 감면"}],
            "discrepancies": [], "not_found": ["이자율"], "cautions": []}
    base.update(over)
    return lambda system, user: base


def run(chat, fetch=fetch_ok, search=lambda q: [URL]):
    return answer_research.research(SERVICE, search=search, fetch=fetch, chat_json=chat, today=date(2026, 10, 29))


def test_fills_verified_values_with_source_and_notes():
    res = run(llm())
    assert res["ok"], res
    d = res["data"]
    assert d["source"]["url"] == URL and d["source"]["level"] == "공식" and "원금의 20~70%" in d["source"]["quote"]
    assert d["amounts"] == [{"label": "원금 감면", "text": "원금의 20~70%", "condition": ""}]
    assert d["support_lines"] == [{"label": "분할상환", "text": "최장 10년간 나눠 갚아요."}]
    assert any("확인하지 못함: 이자율" in n for n in d["notes"]) and "담당 기관에서 확인" in d["notes"][-1] and "2026-10-29" in d["notes"][-1]
    assert answer_override.validate(d)[0] is not None and len(d["notes"]) <= answer_override.MAX_NOTES


def test_drops_fabricated_quote_and_foreign_numbers():
    res = run(llm(amounts=[{"label": "지어낸 금액", "text": "월 50만원", "condition": "", "source": 1, "quote": "월 50만원을 지급한다"}],
                  support_lines=[{"label": "감면", "text": "원금의 90%를 감면해요.", "source": 1, "quote": "원금의 20~70% 및 이자 전액 감면"}],
                  benefit_text="월 100만원을 드려요.", needs_check=["만 35세 이하"]))
    d = res["data"]
    assert d["amounts"] == [] and d["source"] == {} and d["support_lines"] == [] and d["benefit_text"] == "" and d["needs_check"] == []
    assert len(res["issues"]) == 4


def test_collected_text_cannot_back_an_amount():
    res = run(llm(amounts=[{"label": "감면", "text": "채무감면", "condition": "", "source": 0, "quote": "채무감면 및 분할상환"}]))
    assert res["data"]["amounts"] == []


def test_blocked_pages_are_recorded_and_nothing_is_saved():
    def blocked(url):
        raise page_fetch.FetchError("network", "ConnectionError")
    res = run(llm(), fetch=blocked)
    assert not res["ok"] and res["data"] is None and res["unread"][0]["cause"] == "network" and "붙여넣기" in res["message"]


def test_irrelevant_pages_are_not_used():
    assert not run(llm(relevant=[0]))["ok"]


def test_search_failure_still_tries_detail_url():
    def broken(q):
        raise RuntimeError("no key")
    svc = {**SERVICE, "detail_url": URL}
    res = answer_research.research(svc, search=broken, fetch=fetch_ok, chat_json=llm(), today=date(2026, 10, 29))
    assert res["ok"] and "웹 검색 실패: no key" in res["data"]["ai_memo"]


def test_second_level_source_is_marked():
    res = run(llm(), fetch=lambda u: page_fetch.page_from_text(u, "블로그", BODY), search=lambda q: ["https://blog.example.com/a"])
    assert res["data"]["source"]["level"] == "2차" and "2차 자료 기준" in res["data"]["notes"][-1]


def test_official_domains_sort_first():
    urls, _ = answer_research.candidate_urls(SERVICE, search=lambda q: ["https://blog.example.com/a", URL])
    assert urls[0] == URL


def test_web_search_urls_keep_only_citations_and_strip_utm(monkeypatch):
    ann = [SimpleNamespace(type="url_citation", url="https://www.gov.kr/p?id=1&utm_source=openai"), SimpleNamespace(type="file", url="x")]
    resp = SimpleNamespace(output=[SimpleNamespace(content=[SimpleNamespace(annotations=ann)])])
    calls = []

    class Client:
        responses = SimpleNamespace(create=lambda **kw: calls.append(kw["tools"][0]["type"]) or (_ for _ in ()).throw(RuntimeError("x")) if len(calls) == 0 else resp)

    monkeypatch.setattr(answer_research, "_client", Client())
    assert answer_research._search_openai("q") == ["https://www.gov.kr/p?id=1"]
    assert calls == ["web_search"]        # 첫 도구 이름이 실패하면 web_search_preview로 다시 시도한다


def test_batch_saves_as_ai_and_skips_human_overrides(tmp_path):
    answer_override.save("S2", "human", {"benefit_text": "사람이 쓴 문구"}, tmp_path)
    s2 = {**SERVICE, "service_id": "S2"}
    seen = []
    results = answer_research.run_batch([SERVICE, s2], "taegyu", folder=tmp_path, progress=lambda i, n, name: seen.append((i, n)),
                                        search=lambda q: [URL], fetch=fetch_ok, chat_json=llm(), today=date(2026, 10, 29))
    assert [r["ok"] for r in results] == [True, False] and results[1]["skipped"] and seen == [(1, 2), (2, 2)]
    ov = answer_override.overrides(tmp_path)
    assert ov["S1"]["ai"] and answer_override.pending_ai(ov["S1"]) and ov["S2"]["benefit_text"] == "사람이 쓴 문구"


def test_batch_continues_after_an_error_and_caps_size(tmp_path):
    def chat(system, user):
        raise ValueError("boom")
    many = [{**SERVICE, "service_id": f"S{i}"} for i in range(answer_research.MAX_BATCH + 2)]
    results = answer_research.run_batch(many, "taegyu", folder=tmp_path, search=lambda q: [URL], fetch=fetch_ok, chat_json=chat)
    assert len(results) == answer_research.MAX_BATCH + 1 and "AI 호출에 실패" in results[0]["message"] and "한 번에" in results[-1]["message"]
    assert not answer_override.overrides(tmp_path)


def test_batch_rejects_bad_editor(tmp_path):
    with pytest.raises(ValueError):
        answer_research.run_batch([SERVICE], "한글", folder=tmp_path)


def test_later_search_rounds_find_other_pages_when_the_first_is_blocked():
    asked = []

    def search(q):
        asked.append(q)
        return ["https://www.gov.kr/blocked"] if len(asked) == 1 else [URL]

    def fetch(url):
        if "blocked" in url:
            raise page_fetch.FetchError("robots")
        return fetch_ok(url)
    res = run(llm(), fetch=fetch, search=search)
    assert res["ok"] and len(asked) == 3 and res["read"] == [URL] and res["unread"][0]["cause"] == "robots"


def test_failure_message_shows_search_outcome():
    def broken(q):
        raise RuntimeError("tool not enabled")
    res = run(llm(), search=broken)
    assert not res["ok"] and "웹 검색 오류: tool not enabled" in res["message"]
    res = run(llm(), search=lambda q: [])
    assert "찾은 주소 0개" in res["message"]


def _cli_proc(result, code=0, is_error=False):
    import json
    from types import SimpleNamespace as NS
    return NS(returncode=code, stdout=json.dumps({"result": result, "is_error": is_error}), stderr="")


def test_claude_cli_search_extracts_urls_and_passes_query_as_one_argument(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _cli_proc('찾았어요 ["https://www.gov.kr/p?id=1&utm_source=x", "https://www.kamco.or.kr/a.do."] 끝')
    monkeypatch.setattr(answer_research.shutil, "which", lambda n: "/bin/claude")
    monkeypatch.setattr(answer_research.subprocess, "run", fake_run)
    urls = answer_research._search_claude_cli("국민행복기금; rm -rf /")
    assert urls == ["https://www.gov.kr/p?id=1", "https://www.kamco.or.kr/a.do"]
    assert seen["cmd"][:2] == ["/bin/claude", "-p"] and seen["cmd"][2].endswith("국민행복기금; rm -rf /") and "WebSearch" in seen["cmd"] and "shell" not in seen["kw"]


def test_claude_cli_errors_become_runtime_errors(monkeypatch):
    monkeypatch.setattr(answer_research.shutil, "which", lambda n: "/bin/claude")
    monkeypatch.setattr(answer_research.subprocess, "run", lambda *a, **k: _cli_proc("rate limited", 1, True))
    with pytest.raises(RuntimeError, match="claude 실행 실패"):
        answer_research._search_claude_cli("q")
    monkeypatch.setattr(answer_research.shutil, "which", lambda n: None)
    with pytest.raises(RuntimeError, match="찾지 못"):
        answer_research._search_claude_cli("q")


def test_anthropic_search_reads_tool_result_urls(monkeypatch):
    from types import SimpleNamespace as NS
    resp = NS(content=[NS(type="text", text="..."), NS(type="web_search_tool_result", content=[NS(url="https://www.gov.kr/x?utm_source=a"), NS(url="https://www.moel.go.kr/y")])])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setitem(__import__("sys").modules, "anthropic", NS(Anthropic=lambda **kw: NS(messages=NS(create=lambda **k: resp))))
    assert answer_research._search_anthropic("q") == ["https://www.gov.kr/x", "https://www.moel.go.kr/y"]


def test_auto_search_falls_back_and_reports_every_failure(monkeypatch):
    monkeypatch.delenv("RESEARCH_SEARCH", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(answer_research.shutil, "which", lambda n: "/bin/claude")
    monkeypatch.setattr(answer_research.config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(answer_research, "_search_claude_cli", lambda q: (_ for _ in ()).throw(RuntimeError("no login")))
    monkeypatch.setattr(answer_research, "_search_anthropic", lambda q: ["https://www.gov.kr/ok"])
    assert answer_research.web_search_urls("q") == ["https://www.gov.kr/ok"]
    monkeypatch.setattr(answer_research, "_search_anthropic", lambda q: (_ for _ in ()).throw(RuntimeError("limit")))
    with pytest.raises(RuntimeError) as e:
        answer_research.web_search_urls("q")
    assert "claude: no login" in str(e.value) and "anthropic: limit" in str(e.value) and "openai" not in str(e.value)


def test_search_choice_is_validated(monkeypatch):
    monkeypatch.setenv("RESEARCH_SEARCH", "bing")
    with pytest.raises(RuntimeError, match="알 수 없는 값"):
        answer_research.web_search_urls("q")


def test_log_shows_search_method_pages_and_writer(monkeypatch):
    monkeypatch.delenv("RESEARCH_SEARCH", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(answer_research.shutil, "which", lambda n: "/bin/claude")
    monkeypatch.setattr(answer_research.config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(answer_research, "_search_claude_cli", lambda q: [URL])
    lines = []
    res = answer_research.research(SERVICE, fetch=fetch_ok, chat_json=llm(), today=date(2026, 10, 29), log=lines.append)
    text = "\n".join(lines)
    assert res["ok"]
    assert "검색 방법 시도: Claude Code(claude -p)" in text and "✔ Claude Code(claude -p)로 주소 1개 찾음" in text
    assert f"페이지 읽는 중: {URL}" in text and "✔ 읽음: 가계안정지원사업" in text
    assert "구성 작성 중" in text and "코드 검증" in text


def test_batch_logs_are_streamed_returned_and_kept_in_the_memo(tmp_path):
    live = []
    results = answer_research.run_batch([SERVICE], "taegyu", folder=tmp_path, on_log=lambda i, n, name, line: live.append(line),
                                        search=lambda q: [URL], fetch=fetch_ok, chat_json=llm(), today=date(2026, 10, 29))
    assert results[0]["log"] == live and live[0].endswith("조사 시작") and any("AI 구성으로 저장" in l for l in live)
    assert "■ 진행 기록" in answer_override.overrides(tmp_path)["S1"]["ai_memo"]


def test_search_setup_names_each_method(monkeypatch):
    monkeypatch.setattr(answer_research.shutil, "which", lambda n: None)
    monkeypatch.setattr(answer_research.config, "OPENAI_API_KEY", "k")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text = answer_research.search_setup()
    assert "Claude Code(claude -p) ❌" in text and "Anthropic API(Claude) ❌" in text and "OpenAI ✅" in text and "구성 작성: OpenAI" in text


def test_search_log_reports_skipped_and_failed_methods(monkeypatch):
    monkeypatch.delenv("RESEARCH_SEARCH", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(answer_research.shutil, "which", lambda n: "/bin/claude")
    monkeypatch.setattr(answer_research.config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(answer_research, "_search_claude_cli", lambda q: (_ for _ in ()).throw(RuntimeError("not logged in")))
    lines = []
    with pytest.raises(RuntimeError):
        answer_research.web_search_urls("q", log=lines.append)
    text = "\n".join(lines)
    assert "✘ Claude Code(claude -p) 실패: not logged in" in text and "건너뜀: Anthropic API(Claude) (설치·키 없음)" in text and "건너뜀: OpenAI (설치·키 없음)" in text


def test_claude_cli_not_logged_in_has_a_clear_hint(monkeypatch):
    monkeypatch.setattr(answer_research.shutil, "which", lambda n: "/bin/claude")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return _cli_proc("Not logged in · Please run /login", 1, True)
    monkeypatch.setattr(answer_research.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="로그인되어 있지 않아요.*claude.*login"):
        answer_research._search_claude_cli("q")
    assert seen["encoding"] == "utf-8"        # Windows 기본 인코딩(cp949)으로 읽어 한글이 깨지는 것을 막는다
