"""수집한 원문에 없는 지급 금액을 공식 웹사이트에서 찾아 근거와 함께 돌려준다 (내 조건으로 받을 수 있는 지원의 금액 안내용).

- OpenAI 웹 검색 도구로 찾되, 코드가 다음을 모두 확인한 것만 쓴다: (1) 근거 URL이 실제 검색 인용(annotation)에 있다
  (2) 도메인이 공식 기관(.go.kr / .gov.kr / korea.kr) (3) 금액의 숫자가 모델이 복사한 근거 구절에 글자 그대로 있다.
  하나라도 어긋나면 그 금액은 버린다 (금액을 안내하지 못했다고 보여 주는 쪽이 지어낸 금액보다 낫다).
- 검색이 막혀 있거나 실패하면 빈 결과를 돌려준다. `WEB_LOOKUP=0` 환경변수로 끈다 (과금·네트워크 제한 환경).
- 판정 기준 문서(`data/guides`)는 아직 파싱 전이라 여기서 인용하지 않는다."""
from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urlparse

from src.common import config

log = logging.getLogger(__name__)

ALLOWED_SUFFIXES = (".go.kr", ".gov.kr", "korea.kr")
MAX_QUOTE, MAX_AMOUNT = 300, 80

PROMPT = """너는 한국 정부 지원제도의 지급 금액을 공식 웹사이트에서 찾는다. 웹 검색으로 정부·공공기관 공식 페이지(go.kr, korea.kr 등)만 근거로 삼는다.
제도 이름과 알고 싶은 지원 항목 목록이 주어진다. 각 항목의 지급 금액(원·%·횟수·기간 포함)을 찾을 수 있을 때만 답한다.
JSON 객체 하나만 출력한다:
{"items": [{"index": 항목 번호, "amount": 한 줄 금액 안내(예: "월 최대 20만원, 12개월"), "quote": 페이지에서 글자 그대로 복사한 근거 문장, "url": 근거 페이지 주소}]}
찾지 못한 항목은 빼고, 추측하거나 계산해서 만든 금액은 쓰지 않는다. 블로그·뉴스·요약 사이트는 근거로 쓰지 않는다."""


def enabled() -> bool:
    return os.getenv("WEB_LOOKUP", "1") != "0" and bool(config.OPENAI_API_KEY)


def allowed(url: str) -> bool:
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and any(host == s.lstrip(".") or host.endswith(s) for s in ALLOWED_SUFFIXES)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d+(?:[.,]\d+)*", text or "")}


def _norm(url: str) -> str:
    """검색 도구가 붙이는 추적 파라미터(?utm_source=openai)와 끝 슬래시를 떼고 비교한다."""
    parts = urlparse((url or "").strip())
    return f"{parts.scheme}://{(parts.hostname or '').lower()}{parts.path.rstrip('/')}"


def validate(raw: dict, cited_urls: set[str], n_labels: int, page_text: str = "") -> dict[int, dict]:
    """모델 출력 -> {항목 번호(1부터): {amount, quote, url}}. 위 세 조건을 통과한 것만 남긴다.
    page_text가 있으면 근거 구절이 그 본문에 글자 그대로(공백 무시) 있어야 한다 (직접 읽은 공식 페이지용)."""
    cited = {_norm(u) for u in cited_urls}
    flat = re.sub(r"\s+", "", page_text)
    out: dict[int, dict] = {}
    for item in (raw.get("items") or []):
        if not isinstance(item, dict):
            continue
        idx, url = item.get("index"), str(item.get("url") or "").strip()
        amount, quote = str(item.get("amount") or "").strip(), str(item.get("quote") or "").strip()
        if not isinstance(idx, int) or isinstance(idx, bool) or not 1 <= idx <= n_labels or idx in out:
            continue
        if not amount or not quote or _norm(url) not in cited or not allowed(url):
            continue
        if page_text and re.sub(r"\s+", "", quote) not in flat:
            continue
        nums = _numbers(amount)
        if not nums or not nums <= _numbers(quote):
            continue
        out[idx] = {"amount": amount[:MAX_AMOUNT], "quote": quote[:MAX_QUOTE], "url": url}
    return out


def _search(service_name: str, labels: list[str]) -> tuple[dict, set[str]]:
    from openai import OpenAI
    resp = OpenAI(api_key=config.OPENAI_API_KEY, timeout=60).responses.create(
        model=config.GENERATION_MODEL,
        tools=[{"type": "web_search_preview"}],
        instructions=PROMPT,
        input=json.dumps({"제도 이름": service_name, "항목": [f"{i}. {s}" for i, s in enumerate(labels, start=1)]}, ensure_ascii=False),
    )
    text = (getattr(resp, "output_text", "") or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    urls = {getattr(a, "url", "") for o in getattr(resp, "output", []) or [] if getattr(o, "type", "") == "message"
            for c in getattr(o, "content", []) or [] for a in getattr(c, "annotations", []) or []
            if getattr(a, "type", "") == "url_citation"}
    return (json.loads(match.group(0)) if match else {}), urls


def lookup(service_name: str, labels: list[str]) -> dict[int, dict]:
    """labels(지원 항목 이름들)의 금액을 공식 웹사이트에서 찾는다. 실패·비활성이면 {}."""
    if not labels or not enabled():
        return {}
    try:
        raw, urls = _search(service_name, labels)
        return validate(raw, urls, len(labels))
    except Exception as e:      # noqa: BLE001 — 네트워크·도구 미지원·JSON 오류. 금액 없이 안내한다
        log.warning("웹 금액 조회 실패(%s): %s", service_name, e)
        return {}


PAGE_PROMPT = """너는 정부 지원제도 공식 페이지 본문에서 지급 금액을 찾는다. 입력은 제도 이름, 지원 항목 목록, [본문]이다.
JSON 객체 하나만 출력한다: {"items": [{"index": 항목 번호, "amount": 한 줄 금액 안내, "quote": [본문]에서 글자 그대로 복사한 근거 문장}]}
[본문]에 금액이 명시된 항목만 쓴다. 계산·추측하지 않고, 없으면 items를 비운다."""
MAX_PAGE_CHARS, FETCH_TIMEOUT = 12000, 10


def _fetch_text(url: str) -> str:
    import requests
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts, self.skip = [], 0

        def handle_starttag(self, tag, attrs):
            self.skip += tag in ("script", "style")

        def handle_endtag(self, tag):
            self.skip -= tag in ("script", "style") and self.skip > 0

        def handle_data(self, data):
            if not self.skip and data.strip():
                self.parts.append(data.strip())

    resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    parser = _Text()
    parser.feed(resp.text)
    return "\n".join(parser.parts)[:MAX_PAGE_CHARS]


def page_lookup(service_name: str, labels: list[str], urls: list[str], chat_json=None) -> dict[int, dict]:
    """웹 검색이 금액을 못 찾았을 때의 두 번째 방법: 수집 데이터가 가리키는 공식 페이지(상세 링크 등)를 직접 읽어 금액을 찾는다.
    공식 도메인만 읽고, 근거 구절이 읽은 본문에 글자 그대로 있고 금액 숫자가 그 구절에 있어야 쓴다. 실패하면 {}."""
    if not labels or os.getenv("WEB_LOOKUP", "1") == "0":
        return {}
    if chat_json is None:
        from src.generate import condition_extractor
        chat_json = condition_extractor._chat_json
    for url in dict.fromkeys(u for u in urls if allowed(u)):
        try:
            text = _fetch_text(url)
            if not re.search(r"\d", text):
                continue
            user = json.dumps({"제도 이름": service_name, "항목": [f"{i}. {s}" for i, s in enumerate(labels, start=1)], "본문": text}, ensure_ascii=False)
            raw = chat_json(PAGE_PROMPT, user)
            for item in raw.get("items") or []:
                if isinstance(item, dict):
                    item["url"] = url                      # 읽은 페이지가 곧 출처
            found = validate(raw, {url}, len(labels), page_text=text)
            if found:
                return found
        except Exception as e:      # noqa: BLE001 — 접속·파싱·JSON 오류는 다음 페이지로
            log.warning("공식 페이지 조회 실패(%s): %s", url, e)
    return {}
