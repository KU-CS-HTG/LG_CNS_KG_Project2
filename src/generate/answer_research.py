"""제도를 조사해 '답변 구성'을 AI가 채운다 (화면: `python -m app.amount_mode`에서 제도 체크 -> 'AI로 답변 채우기').

흐름 (제도마다): 웹 검색으로 공식 자료 후보 주소를 찾고 -> 주소를 실제로 열어 읽고(`page_fetch`: robots.txt 준수·요청 간격, 막히면 우회하지 않고 '못 읽음'으로 기록)
-> 읽은 본문과 수집 원문(보조금24)만 근거로 LLM이 구성을 쓰고 -> 코드가 근거 구절과 숫자를 본문과 대조해 어긋난 항목은 버리고
-> `answer_override.save(..., ai=True)`로 저장한다 (사람이 확인 완료하기 전에는 실제 답변에 나오지 않는다).

원칙: 검색 결과의 요약·발췌는 근거로 쓰지 않는다 (주소를 찾는 데만 쓴다). 열어 읽은 본문에 글자 그대로 있는 구절·숫자만 남긴다.
사람이 직접 구성해 둔 제도는 건드리지 않고 건너뛴다. 읽은 자료가 하나도 없으면 아무것도 저장하지 않는다.
자료 간 불일치·확인하지 못한 항목·읽지 못한 자료는 `ai_memo`(관리 화면 전용)와 안내 메모에 남긴다."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.common import config
from src.generate import amount_from_url, answer_override, page_fetch

MAX_URLS, MAX_PAGES, MIN_PAGES, MAX_PAGE_CHARS, MAX_BATCH = 6, 4, 2, 9000, 10
SOURCE_TEXT_KEYS = (("지원대상", "support_target"), ("선정기준", "selection_criteria"), ("지원내용", "support_content"),
                    ("신청방법", "how_to_apply"), ("신청기한", "application_period"))
FINAL_NOTE = "최종 신청 자격과 조건은 담당 기관에서 확인해 주세요."
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

PROMPT = """너는 정부 지원제도 하나의 '답변 카드' 문구를 쓴다. JSON 객체 하나만 출력한다.
입력은 제도 이름, [자료] 목록이다. 자료 0번은 수집 원문(보조금24, 공식), 1번부터는 실제로 열어 읽은 웹페이지다.
자료에 적힌 내용만 쓴다. 자료에 없는 금액·기간·조건·법령 번호를 기억이나 추측으로 채우지 않는다. 찾지 못한 내용은 not_found에 적는다.

출력 형식:
{"relevant": 이 제도를 설명하는 자료 번호들 [int],
 "benefit_text": 지원 내용을 한두 문장으로 (금액·기간은 자료에 있는 값만),
 "how_to_apply": 신청 경로·순서·선행 절차를 순서대로 (①②… 형식 가능),
 "needs_check": 사용자가 체크할 조건 문장들 (나이·소득·재산·취업 상태·제외 사유 등). 조건 값을 확인하지 못했으면 문장 끝에 '(확인 필요)',
 "support_lines": [{"label": 수당·혜택 이름, "text": 내용 한 문장, "source": 자료 번호, "quote": 그 자료에서 글자 그대로 복사한 구절}],
 "amounts": [{"label": 이름, "text": 금액·비율·기간 문구, "condition": 적용 조건(없으면 ""), "source": 1 이상의 자료 번호, "quote": 그 자료에서 글자 그대로 복사한 구절}],
 "discrepancies": [자료끼리 값이 다른 경우 각 자료의 값을 적은 문장],
 "not_found": [확인하지 못한 항목],
 "cautions": [중복 수급·참여 제한, 구 자료와의 차이 등 사용자가 알아야 할 점]}

규칙:
- quote는 해당 자료 본문에서 한 글자도 바꾸지 말고 복사한다. 줄이거나 고쳐 쓰지 않는다.
- 자료마다 수치가 다르면 하나를 고르지 말고 discrepancies에 둘 다 적는다 (본문 문구에는 어느 쪽도 단정하지 않는다).
- 자격이 되는지 단정하지 않는다. 한국어 존댓말로 짧게 쓴다.
- support_lines는 혜택마다 한 줄이다. amounts는 자료에 금액·비율·기간이 명시된 것만 넣고, 없으면 빈 배열."""

_client = None
CLAUDE_TIMEOUT, ANTHROPIC_MODEL = 240, "claude-sonnet-5-5"


def _nums(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM_RE.findall(text or "")}


def _level(url: str) -> str:
    """자료 등급: 정부 도메인이거나 확인된 공식 기관 호스트(page_fetch.OFFICIAL_HOSTS)면 '공식', 아니면 '2차'."""
    host = (urlparse(url).hostname or "").lower()
    return "공식" if amount_from_url.default_level(url) == "공식" or host in page_fetch.OFFICIAL_HOSTS else "2차"


def _clean_url(url: str) -> str:
    """검색 결과 주소에서 추적용 파라미터(utm_*)를 뗀다."""
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")])
    return urlunparse(p._replace(query=query, fragment=""))


def _search_openai(query: str) -> list[str]:
    """OpenAI Responses의 web_search 도구로 주소를 찾는다. 실패하면 RuntimeError."""
    global _client
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        from openai import OpenAI
        _client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=90)
    model, last = os.environ.get("RESEARCH_MODEL") or config.GENERATION_MODEL, None
    for tool in ("web_search", "web_search_preview"):
        try:
            resp = _client.responses.create(model=model, tools=[{"type": tool}], input=query)
        except Exception as exc:     # noqa: BLE001 — 도구 이름·모델 미지원·네트워크 오류: 다음 이름을 시도한다
            last = exc
            continue
        urls: list[str] = []
        for item in getattr(resp, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                for ann in getattr(part, "annotations", None) or []:
                    u = getattr(ann, "url", None)
                    if getattr(ann, "type", "") == "url_citation" and u:
                        urls.append(_clean_url(u))
        return list(dict.fromkeys(urls))
    raise RuntimeError(f"웹 검색에 실패했어요: {last!r}")


SEARCH_PROMPT = ("웹 검색으로 아래 정부 지원제도를 설명하는 공식 안내 페이지(정부·공공기관·소관 기관 사이트 우선, 블로그·뉴스는 뒤)를 찾아 "
                 "그 주소만 JSON 배열로 출력해. 설명·요약은 쓰지 마. 예: [\"https://…\", \"https://…\"]\n제도: ")
_URL_RE = re.compile(r"https?://[^\s\"'<>\])},，]+")


def _search_claude_cli(query: str) -> list[str]:
    """이 PC의 Claude Code(`claude -p`)로 웹 검색해 주소만 얻는다 (API 키 없이 로그인된 Claude를 쓴다). 실패하면 RuntimeError."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude 명령을 찾지 못했어요 (Claude Code 설치·로그인 필요).")
    try:
        proc = subprocess.run([exe, "-p", SEARCH_PROMPT + query, "--allowedTools", "WebSearch", "--output-format", "json", "--max-turns", "8"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=CLAUDE_TIMEOUT, cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude 응답이 {CLAUDE_TIMEOUT}초 안에 오지 않았어요.") from exc
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError(f"claude 출력을 읽지 못했어요: {(proc.stderr or proc.stdout)[:200]}") from exc
    if "not logged in" in str(data.get("result") or proc.stderr).lower() or "/login" in str(data.get("result") or ""):
        raise RuntimeError("Claude Code에 로그인되어 있지 않아요. 터미널에서 `claude`를 실행해 `/login`으로 로그인한 뒤 다시 시도하세요.")
    if proc.returncode != 0 or data.get("is_error"):
        raise RuntimeError(f"claude 실행 실패: {str(data.get('result') or proc.stderr)[:200]}")
    return list(dict.fromkeys(_clean_url(u.rstrip(".")) for u in _URL_RE.findall(str(data.get("result") or ""))))


def _search_anthropic(query: str) -> list[str]:
    """Anthropic API의 웹 검색 도구로 주소를 찾는다 (ANTHROPIC_API_KEY와 `pip install anthropic` 필요). 실패하면 RuntimeError."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다 (.env 확인).")
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic 패키지가 없어요 (pip install anthropic).") from exc
    try:
        resp = anthropic.Anthropic(api_key=key, timeout=90).messages.create(
            model=os.environ.get("RESEARCH_MODEL") or ANTHROPIC_MODEL, max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": SEARCH_PROMPT + query}])
    except Exception as exc:     # noqa: BLE001 — 한도·네트워크·모델 오류
        raise RuntimeError(f"anthropic 검색 실패: {exc!r}") from exc
    urls = []
    for block in resp.content:
        if getattr(block, "type", "") == "web_search_tool_result" and isinstance(getattr(block, "content", None), list):
            urls += [_clean_url(r.url) for r in block.content if getattr(r, "url", None)]
    return list(dict.fromkeys(urls))


BACKEND_LABELS = {"claude": "Claude Code(claude -p)", "anthropic": "Anthropic API(Claude)", "openai": "OpenAI"}


def _usable(name: str) -> bool:
    return {"claude": lambda: bool(shutil.which("claude")), "anthropic": lambda: bool(os.environ.get("ANTHROPIC_API_KEY")),
            "openai": lambda: bool(config.OPENAI_API_KEY)}[name]()


def search_setup() -> str:
    """화면에 보일 검색 설정 요약: 어떤 방법을 쓸 수 있고 어떤 순서로 시도하는지."""
    choice = (os.environ.get("RESEARCH_SEARCH") or "auto").lower()
    parts = [f"{BACKEND_LABELS[n]} {'✅' if _usable(n) else '❌ (설치·키 없음)'}" for n in BACKEND_LABELS]
    return f"웹 검색 방법: {' → '.join(parts)} · 모드 {choice}" + ("" if choice == "auto" else " (지정한 방법만 시도)") + f" · 구성 작성: OpenAI {config.GENERATION_MODEL}"


def _say(log, msg: str) -> None:
    if log:
        log(msg)


def web_search_urls(query: str, log=None) -> list[str]:
    """웹 검색으로 자료 후보 주소만 얻는다 (요약·발췌는 쓰지 않는다). 쓸 수 있는 방법을 차례로 시도하고 모두 실패하면 RuntimeError.
    기본(RESEARCH_SEARCH=auto)은 Claude Code(claude 명령) -> Anthropic API(키가 있을 때) -> OpenAI(키가 있을 때). 하나만 쓰려면 claude|anthropic|openai.
    log가 있으면 어떤 방법을 시도했고 어떻게 됐는지 한 줄씩 알린다."""
    choice = (os.environ.get("RESEARCH_SEARCH") or "auto").lower()
    backends = {"claude": _search_claude_cli, "anthropic": _search_anthropic, "openai": _search_openai}
    order = ["claude", "anthropic", "openai"] if choice == "auto" else [choice]
    errors = []
    for name in order:
        if name not in backends:
            raise RuntimeError(f"RESEARCH_SEARCH={choice}는 알 수 없는 값이에요 (auto|claude|anthropic|openai).")
        if choice == "auto" and not _usable(name):
            _say(log, f"검색 방법 건너뜀: {BACKEND_LABELS[name]} (설치·키 없음)")
            continue
        _say(log, f"검색 방법 시도: {BACKEND_LABELS[name]}")
        try:
            urls = backends[name](query)
        except RuntimeError as exc:
            errors.append(f"{name}: {exc}")
            _say(log, f"✘ {BACKEND_LABELS[name]} 실패: {str(exc)[:160]}")
            continue
        _say(log, f"✔ {BACKEND_LABELS[name]}로 주소 {len(urls)}개 찾음")
        return urls
    raise RuntimeError("웹 검색에 실패했어요 — " + (" / ".join(errors) or "쓸 수 있는 검색 방법이 없어요 (claude 명령 설치, ANTHROPIC_API_KEY 또는 OPENAI_API_KEY 필요)."))


def queries(service: dict) -> list[str]:
    """검색 질문들. 앞 질문으로 읽은 자료가 모자라면 다음 질문으로 다른 자료를 찾는다."""
    name, agency = service.get("service_name", ""), service.get("agency", "")
    return [f"{name} {agency} 지원대상 지원내용 신청방법".strip(), f"{name} 신청 자격 지원 금액 기간 정부24 복지로 고용24",
            f"{name} {agency} 공고 사업 안내 시행계획".strip()]


def candidate_urls(service: dict, search=None, round_: int = 0, log=None) -> tuple[list[str], str]:
    """round_번째 질문으로 읽어 볼 주소 목록(공식 도메인 먼저, 최대 MAX_URLS개)과 검색 실패 메시지(성공이면 ''). 첫 라운드에는 제도 상세 링크도 넣는다."""
    found: list[str] = []
    error = ""
    query = queries(service)[round_]
    _say(log, f"웹 검색 {round_ + 1}번째 질문: {query}")
    try:
        found = search(query) if search else web_search_urls(query, log)
    except Exception as exc:     # noqa: BLE001 — 검색이 안 돼도 상세 링크는 시도한다
        error = str(exc)
    detail = service.get("detail_url") or ""
    urls = [u for u in dict.fromkeys(([detail] if round_ == 0 and detail.startswith(("http://", "https://")) else []) + found) if u]
    urls.sort(key=lambda u: _level(u) != "공식")      # 안정 정렬: 공식 도메인이 앞, 같은 등급은 검색 순서 유지
    _say(log, f"읽어 볼 후보 주소 {len(urls[:MAX_URLS])}개 (공식 도메인 {sum(1 for u in urls[:MAX_URLS] if _level(u) == '공식')}개)")
    return urls[:MAX_URLS], error


def read_pages(urls: list[str], fetch=None, log=None) -> tuple[list[page_fetch.Page], list[dict]]:
    """주소를 열어 읽는다 -> (읽은 페이지, 못 읽은 기록). 막힌 사이트는 우회하지 않고 원인만 남긴다."""
    pages, unread = [], []
    for url in urls:
        if len(pages) >= MAX_PAGES:
            break
        _say(log, f"페이지 읽는 중: {url}")
        try:
            page = (fetch or page_fetch.fetch)(url)
        except page_fetch.FetchError as exc:
            unread.append({"url": url, "cause": exc.cause, "message": str(exc)})
            _say(log, f"✘ 못 읽음 [{exc.cause}] (우회하지 않음)")
            continue
        except RuntimeError as exc:
            unread.append({"url": url, "cause": "other", "message": str(exc)})
            _say(log, f"✘ 못 읽음 [other] {str(exc)[:100]}")
            continue
        _say(log, f"✔ 읽음: {page.title or '(제목 없음)'} · {len(page.text):,}자")
        if fetch is None:
            page_fetch.save_snapshot(page)       # 나중에 근거 구절을 `page_fetch verify`로 다시 대조할 수 있게
        pages.append(page)
    return pages, unread


def source_text(service: dict) -> str:
    parts = [f"{label}: {str(service.get(key) or '').strip()}" for label, key in SOURCE_TEXT_KEYS if str(service.get(key) or "").strip()]
    return f"제도 이름: {service.get('service_name', '')}\n" + "\n".join(parts)


def _grounded(quote, text: str) -> bool:
    return isinstance(quote, str) and page_fetch.quote_in_text(text, quote)


def _numbers_ok(value: str, quote: str) -> bool:
    return _nums(value) <= _nums(quote)


def verify(raw: dict, texts: list[str], titles: list[str], urls: list[str]) -> tuple[dict, list[str]]:
    """LLM 출력을 코드로 검증한다. texts[0]은 수집 원문, 1~는 읽은 페이지. -> (정리된 값, 뺀 항목 설명).
    근거 구절은 인용한 자료 본문에 글자 그대로 있어야 하고, 항목의 숫자는 그 구절에 있어야 한다. 금액 안내는 읽은 웹페이지(1~)만 근거가 된다."""
    issues: list[str] = []
    n = len(texts)
    relevant = {i for i in (raw.get("relevant") or []) if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n}
    all_nums = _nums("\n".join(texts[i] for i in sorted(relevant)))

    def src_ok(item: dict, min_idx: int) -> int | None:
        i = item.get("source")
        if isinstance(i, bool) or not isinstance(i, int) or not min_idx <= i < n or i not in relevant:
            return None
        return i if _grounded(item.get("quote"), texts[i]) else None

    support = []
    for it in (raw.get("support_lines") or []):
        if not isinstance(it, dict) or not str(it.get("label") or "").strip() or not str(it.get("text") or "").strip():
            continue
        i = src_ok(it, 0)
        if i is None or not _numbers_ok(f"{it['label']} {it['text']}", it["quote"]):
            issues.append(f"지원 내용 '{str(it.get('label'))[:20]}': 근거 구절이나 숫자가 자료와 맞지 않아 뺐어요.")
            continue
        support.append({"label": str(it["label"]).strip(), "text": str(it["text"]).strip(), "source": i, "quote": it["quote"].strip()})
    amounts = []
    for it in (raw.get("amounts") or []):
        if not isinstance(it, dict) or not str(it.get("label") or "").strip() or not str(it.get("text") or "").strip():
            continue
        i = src_ok(it, 1)
        if i is None or not _numbers_ok(f"{it['text']} {it.get('condition') or ''}", it["quote"]):
            issues.append(f"금액 안내 '{str(it.get('label'))[:20]}': 읽은 자료의 근거 구절이나 숫자가 맞지 않아 뺐어요.")
            continue
        amounts.append({"label": str(it["label"]).strip(), "text": str(it["text"]).strip(), "condition": str(it.get("condition") or "").strip(),
                        "source": i, "quote": it["quote"].strip()})
    out = {"relevant": sorted(relevant), "support_lines": support, "amounts": amounts}
    for key in ("benefit_text", "how_to_apply"):
        value = str(raw.get(key) or "").strip()
        if value and not _nums(value) <= all_nums:
            issues.append(f"'{key}'의 숫자가 자료에 없어 비웠어요 (바꾸지 않음으로 처리).")
            value = ""
        out[key] = value
    needs = []
    for line in (raw.get("needs_check") or []):
        line = str(line).strip()
        if not line:
            continue
        if _nums(line) <= all_nums:
            needs.append(line)
        else:
            issues.append(f"체크리스트 '{line[:25]}…': 숫자가 자료에 없어 뺐어요.")
    out["needs_check"] = needs
    for key in ("discrepancies", "not_found", "cautions"):
        out[key] = [str(x).strip() for x in (raw.get(key) or []) if str(x).strip()]
    return out, issues


def _pick_source(amounts: list[dict], urls: list[str]) -> int | None:
    """금액 안내의 출처로 쓸 자료: 근거가 확인된 금액이 가장 많은 자료 (같으면 공식 도메인)."""
    if not amounts:
        return None
    count: dict[int, int] = {}
    for a in amounts:
        count[a["source"]] = count.get(a["source"], 0) + 1
    return max(count, key=lambda i: (count[i], _level(urls[i]) == "공식"))


def build(service: dict, pages: list[page_fetch.Page], unread: list[dict], search_error: str, chat_json=None, today: date | None = None, log=None) -> dict:
    """읽은 자료로 구성을 만든다 -> {ok, message, data, issues}. data는 answer_override.save가 받는 형식이다."""
    today = today or date.today()
    injected = chat_json
    chat_json = chat_json or __import__("src.generate.condition_extractor", fromlist=["_chat_json"])._chat_json
    texts = [source_text(service)] + [p.text[:MAX_PAGE_CHARS] for p in pages]
    titles = ["수집 원문(보조금24)"] + [p.title for p in pages]
    urls = [""] + [p.url for p in pages]
    user = json.dumps({"제도 이름": service.get("service_name", ""),
                       "자료": [{"번호": i, "제목": titles[i], "본문": texts[i]} for i in range(len(texts))]}, ensure_ascii=False)
    _say(log, f"구성 작성 중: {'OpenAI ' + config.GENERATION_MODEL if injected is None else '주입된 LLM'}가 읽은 자료 {len(pages)}개 + 수집 원문으로 작성")
    try:
        raw = chat_json(PROMPT, user)
    except Exception as exc:     # noqa: BLE001 — 키 없음·네트워크·JSON 오류를 화면에 알린다
        _say(log, f"✘ 작성 실패: {str(exc)[:160]}")
        return {"ok": False, "message": f"AI 호출에 실패했어요: {exc!r}", "data": None, "issues": []}
    v, issues = verify(raw, texts, titles, urls)
    _say(log, f"코드 검증: 근거 구절·숫자를 본문과 대조해 {len(issues)}개 항목을 뺐어요" if issues else "코드 검증: 모든 항목의 근거 구절·숫자가 본문과 맞아요")
    if not any(i >= 1 for i in v["relevant"]):
        return {"ok": False, "message": "읽은 자료 중 이 제도를 설명하는 페이지가 없어 채우지 않았어요.", "data": None, "issues": issues}
    pick = _pick_source(v["amounts"], urls)
    amounts, spare = [a for a in v["amounts"] if a["source"] == pick], [a for a in v["amounts"] if a["source"] != pick]
    source = {}
    if pick is not None:
        source = {"title": titles[pick] or service.get("service_name", ""), "url": urls[pick], "level": _level(urls[pick]),
                  "quote": "\n".join(dict.fromkeys(a["quote"] for a in amounts))}
    read_levels = {_level(u) for u in urls[1:]}
    notes = []
    flags = v["discrepancies"] + [f"확인하지 못함: {x}" for x in v["not_found"]]
    if flags:
        notes.append("확인할 점: " + " / ".join(flags))
    if v["cautions"]:
        notes.append(" ".join(v["cautions"]))
    if "공식" not in read_levels or (source and source["level"] == "2차"):
        notes.append(f"2차 자료 기준이에요 (공식 자료를 읽지 못함). 자료 확인일 {today.isoformat()}. {FINAL_NOTE}")
    else:
        notes.append(f"자료 확인일 {today.isoformat()}. {FINAL_NOTE}")
    notes = [n[:answer_override.MAX_NOTE] for n in notes]      # 최대 3개: 확인할 점 · 주의 · 출처 수준과 최종 확인 안내
    data = {"benefit_text": v["benefit_text"], "how_to_apply": v["how_to_apply"], "needs_check": v["needs_check"][:answer_override.MAX_NEEDS],
            "notes": notes, "amounts": [{k: a[k] for k in ("label", "text", "condition")} for a in amounts],
            "amount_title": "", "support_lines": [{"label": s["label"], "text": s["text"]} for s in v["support_lines"]][:answer_override.MAX_SUPPORT],
            "ai_memo": _memo(service, pages, unread, search_error, v, issues, spare, source, today), "source": source}
    return {"ok": True, "message": f"읽은 자료 {len(pages)}개로 채웠어요.", "data": data, "issues": issues}


def _memo(service, pages, unread, search_error, v, issues, spare, source, today) -> str:
    lines = [f"[조사 기록 — 확인일 {today.isoformat()} / 이 메모는 사용자 화면에 나오지 않아요]", "■ 읽은 자료"]
    lines += [f"- {p.title or '(제목 없음)'} · {p.url} · {_level(p.url)} · 읽은 시각 {p.fetched_at[:16]}" for p in pages]
    if unread or search_error:
        lines.append("■ 읽지 못한 자료·검색 문제")
        lines += [f"- {u['url']} · {u['cause']}" for u in unread]
        if search_error:
            lines.append(f"- 웹 검색 실패: {search_error[:150]}")
    if issues:
        lines.append("■ 코드 검증에서 뺀 항목 (근거 구절·숫자가 자료와 맞지 않음)")
        lines += [f"- {i}" for i in issues]
    for title, items in (("자료 간 불일치", v["discrepancies"]), ("확인하지 못한 항목", v["not_found"]), ("주의", v["cautions"])):
        if items:
            lines += [f"■ {title}"] + [f"- {x}" for x in items]
    if spare:
        lines.append("■ 다른 자료에서 확인된 금액 (금액 안내의 출처는 한 자료만 쓸 수 있어 여기에만 남김)")
        lines += [f"- {a['label']}: {a['text']} · 근거: {a['quote'][:80]}" for a in spare]
    lines.append("■ 사람이 확인할 것: 위 자료의 공식 원문과 금액·기간을 한 번 더 대조한 뒤 '확인 완료'를 눌러 주세요.")
    return "\n".join(lines)[:answer_override.MAX_MEMO]


def research(service: dict, *, search=None, fetch=None, chat_json=None, today: date | None = None, log=None) -> dict:
    """제도 하나를 조사해 구성을 만든다 (저장하지 않음) -> {ok, message, data, issues, read, unread}.
    읽은 자료가 MIN_PAGES개 모자라면(막힌 사이트가 많으면) 다른 검색 질문으로 다른 자료를 찾아 더 읽는다 (이미 시도한 주소는 다시 읽지 않는다)."""
    pages: list[page_fetch.Page] = []
    unread: list[dict] = []
    tried: set[str] = set()
    errors: list[str] = []
    found = 0
    for round_ in range(len(queries(service))):
        urls, error = candidate_urls(service, search, round_, log)
        found += len([u for u in urls if u != service.get("detail_url")])
        if error:
            errors.append(error)
        more, bad = read_pages([u for u in urls if u not in tried], fetch, log)
        tried.update(urls)
        pages += more
        unread += bad
        if len(pages) >= MIN_PAGES:
            break
    search_error = "; ".join(dict.fromkeys(errors))
    if not pages:
        why = "; ".join(f"{u['url']} [{u['cause']}]" for u in unread[:3]) or "후보 주소가 없어요"
        search_note = f"웹 검색 오류: {search_error[:200]}" if search_error else f"웹 검색으로 찾은 주소 {found}개"
        return {"ok": False, "message": f"읽을 수 있는 자료가 없어 채우지 않았어요 — {search_note} / 읽지 못함: {why}. 접속이 막힌 사이트는 '자료 주소로 채우기'의 본문 붙여넣기를 쓰세요.",
                "data": None, "issues": [], "read": [], "unread": unread}
    res = build(service, pages[:MAX_PAGES], unread, search_error, chat_json, today, log)
    return {**res, "read": [p.url for p in pages[:MAX_PAGES]], "unread": unread}


def run_batch(services: list[dict], editor: str, *, folder: Path | None = None, progress=None, on_log=None, **deps) -> list[dict]:
    """제도들을 차례로 조사해 AI 구성으로 저장한다. 한 제도가 실패해도 다음으로 넘어간다 -> 제도별 결과 목록(진행 기록 `log` 포함).
    사람이 구성해 둔(또는 확인 완료한) 답변이 있는 제도는 건너뛴다 (AI가 채우고 아직 확인 전인 구성만 다시 쓴다).
    on_log(i, n, name, 줄)은 진행 기록이 한 줄 생길 때마다 불린다 (화면에서 실시간으로 보여 주는 데 쓴다). 진행 기록은 AI 조사 메모에도 남는다."""
    if not answer_override.valid_editor(editor):
        raise ValueError("입력자 이름은 영문·숫자·_-만 쓸 수 있어요.")
    results = []
    total = min(len(services), MAX_BATCH)
    for i, s in enumerate(services[:MAX_BATCH], 1):
        sid, name = s["service_id"], s.get("service_name", "")
        lines: list[str] = []
        started = time.monotonic()

        def log(msg: str, _i=i, _name=name, _lines=lines, _t0=started) -> None:
            line = f"[{time.monotonic() - _t0:5.1f}초] {msg}"
            _lines.append(line)
            if on_log:
                on_log(_i, total, _name, line)

        if progress:
            progress(i, total, name)
        existing = answer_override.overrides(folder).get(sid)
        if existing and not answer_override.pending_ai(existing):
            log("건너뜀: 사람이 구성한(또는 확인 완료한) 답변이 있어요")
            results.append({"service_id": sid, "service_name": name, "ok": False, "skipped": True, "log": lines,
                            "message": "사람이 구성한(또는 확인 완료한) 답변이 있어 건너뛰었어요.", "issues": [], "read": [], "unread": []})
            continue
        log("조사 시작")
        try:
            res = research(s, log=log, **deps)
            if res["ok"]:
                log("AI 구성으로 저장 (사람이 '확인 완료'하기 전에는 실제 답변에 나오지 않아요)")
                data = {**res["data"], "ai_memo": (res["data"]["ai_memo"] + "\n■ 진행 기록\n" + "\n".join(lines))[:answer_override.MAX_MEMO]}
                answer_override.save(sid, editor, data, folder, ai=True)
            else:
                log("채우지 않음: " + str(res["message"])[:120])
        except Exception as exc:     # noqa: BLE001 — 한 제도의 오류가 전체 작업을 멈추지 않게
            res = {"ok": False, "message": f"처리 중 오류: {exc!r}", "data": None, "issues": [], "read": [], "unread": []}
            log(f"✘ 오류: {exc!r}"[:160])
        results.append({"service_id": sid, "service_name": name, "skipped": False, "log": lines, **{k: res.get(k) for k in ("ok", "message", "issues", "read", "unread")}})
    if len(services) > MAX_BATCH:
        results.append({"service_id": "", "service_name": "", "ok": False, "skipped": True, "log": [],
                        "message": f"한 번에 {MAX_BATCH}개까지만 처리해요. 나머지 {len(services) - MAX_BATCH}개는 다시 체크해 주세요.", "issues": [], "read": [], "unread": []})
    return results
