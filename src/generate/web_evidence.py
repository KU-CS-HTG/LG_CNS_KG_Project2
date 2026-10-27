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


def validate(raw: dict, cited_urls: set[str], n_labels: int) -> dict[int, dict]:
    """모델 출력 -> {항목 번호(1부터): {amount, quote, url}}. 위 세 조건을 통과한 것만 남긴다."""
    out: dict[int, dict] = {}
    for item in (raw.get("items") or []):
        if not isinstance(item, dict):
            continue
        idx, url = item.get("index"), str(item.get("url") or "").strip()
        amount, quote = str(item.get("amount") or "").strip(), str(item.get("quote") or "").strip()
        if not isinstance(idx, int) or isinstance(idx, bool) or not 1 <= idx <= n_labels or idx in out:
            continue
        if not amount or not quote or url not in cited_urls or not allowed(url):
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
