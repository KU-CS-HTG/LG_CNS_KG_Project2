"""공식 자료 주소(웹페이지)를 읽어 '예상 수령액 규칙 입력 폼'을 미리 채운다 (화면: `python -m app.amount_mode`의 '자료에서 채우기').

사람이 주소만 넣으면 AI가 페이지 본문에서 금액·기간·나이·소득 조건을 찾아 폼 값으로 옮기고, 사람은 세부 내용만 고친다.
AI가 쓴 값은 코드가 페이지 본문과 대조한다: 근거 구절은 본문에 글자 그대로 있어야 하고, 금액·개월 수·비율·한도는 본문에 나온 숫자여야 한다
(없으면 그 유형을 버린다). 나이·중위소득 조건 숫자가 본문에 없으면 조건을 비우고 경고한다. 페이지가 이 제도와 다른 사업이면 채우지 않는다.
저장(규칙 파일에 쓰기)은 여전히 사람이 폼을 확인하고 누른다."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from src.generate import amount_draft, page_fetch

MAX_PAGE_CHARS, MAX_VARIANTS = 15000, 4
OFFICIAL_SUFFIXES = (".go.kr", ".gov.kr", "korea.kr")      # 이 도메인이면 자료 등급을 '공식'으로 미리 고른다 (그 밖은 사람이 고른다)

PROMPT = """너는 정부 지원제도 공식 안내 페이지에서 '예상 수령액 규칙'에 필요한 값을 찾아 폼 값으로 옮긴다. JSON 객체 하나만 출력한다.
입력은 제도 이름, 수집 데이터의 지원내용(참고), [페이지 본문]이다. 페이지 본문에 적힌 값만 쓰고, 없는 금액·조건을 만들지 않는다.

출력 형식:
{"page_matches_service": 이 페이지가 입력한 제도(또는 그 제도의 지원 내용)를 설명하면 true, 다른 사업이면 false,
 "match_reason": 판단 이유 한 문장,
 "variants": [{"name": 지원 유형 이름(한도이면 '최대'나 '한도'를 넣는다), "pay_type": "fixed"|"percent",
               "amount_won": 원 단위 정수(fixed), "rate_pct": 지원 비율 숫자(percent), "cap_won": 한도 원(percent, 없으면 0),
               "months": 지급 개월 수(1회성이면 0), "age_min": 나이 최소(없으면 0), "age_max": 나이 최대(없으면 0),
               "median_pct": 중위소득 N% 이하의 N(없으면 0), "no_house": 무주택 조건이면 true,
               "quote": [페이지 본문]에서 글자 그대로 복사한 근거 구절}],
 "note": 계산에 반영하지 못한 조건·주의(선발형, 예산 한정, 심사 등) 한두 문장}

규칙:
- 금액·비율·기간·나이·소득 숫자는 [페이지 본문]에 있는 값만 쓴다. 계산하거나 추정하지 않는다.
- 사용자 조건에 따라 금액이 다르면 유형(variant)으로 나눈다. 유형은 최대 4개.
- 금액이 없거나 계산할 수 없는 제도면 variants를 빈 배열로 둔다."""


html_to_text = page_fetch.html_to_text
check_url = page_fetch.check_url


def fetch_page(url: str) -> tuple[str, str]:
    """(제목, 본문 글). 읽은 기록을 남기고, 실패하면 원인과 해결 방법을 담은 RuntimeError(page_fetch.FetchError)."""
    page = page_fetch.fetch(url)
    page_fetch.save_snapshot(page)
    return page.title, page.text[:MAX_PAGE_CHARS]


def default_level(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return "공식" if any(host == s.lstrip(".") or host.endswith(s) for s in OFFICIAL_SUFFIXES) else "2차"


def _clean_variant(v: dict, nums: set[float], flat: str, issues: list[str]) -> dict | None:
    name = str(v.get("name") or "").strip()
    quote = str(v.get("quote") or "").strip()
    if not name:
        return None
    if len(amount_draft._squash(quote)) < 4 or amount_draft._squash(quote) not in flat:
        issues.append(f"'{name}': 근거 구절이 페이지 본문에 글자 그대로 없어 이 유형을 뺐어요.")
        return None
    pay = "percent" if v.get("pay_type") == "percent" else "fixed"
    out = {"name": name, "pay_type": pay, "amount_won": 0, "rate_pct": 0.0, "cap_won": 0, "months": 0, "age_min": 0, "age_max": 0,
           "median_pct": 0, "no_house": bool(v.get("no_house")) and "무주택" in quote, "quote": quote}
    num = lambda key: v.get(key) if isinstance(v.get(key), (int, float)) and not isinstance(v.get(key), bool) else 0
    if pay == "fixed":
        if not amount_draft._in(nums, num("amount_won")) or num("amount_won") <= 0:
            issues.append(f"'{name}': 금액이 페이지 본문에 없어 이 유형을 뺐어요.")
            return None
        out["amount_won"] = int(num("amount_won"))
    else:
        rate = float(num("rate_pct"))
        if not 0 < rate <= 100 or not amount_draft._in(nums, rate):
            issues.append(f"'{name}': 지원 비율이 페이지 본문에 없어 이 유형을 뺐어요.")
            return None
        out["rate_pct"] = rate
        if num("cap_won"):
            if amount_draft._in(nums, num("cap_won")):
                out["cap_won"] = int(num("cap_won"))
            else:
                issues.append(f"'{name}': 한도 금액이 본문에 없어 비웠어요. 직접 확인해 입력해 주세요.")
    for key, label in (("months", "지급 개월 수"), ("age_min", "나이 최소"), ("age_max", "나이 최대"), ("median_pct", "중위소득 %")):
        if num(key):
            if amount_draft._in(nums, num(key)):
                out[key] = int(num(key))
            else:
                issues.append(f"'{name}': {label} {num(key)}가 본문에 없어 비웠어요 (조건이 빠지면 더 많은 사람이 해당하는 것으로 계산돼요. 직접 확인해 주세요).")
    return out


def fill_form(service: dict, url: str, chat_json=None, fetch=None) -> dict:
    """주소의 페이지를 읽어 폼 값을 만든다 -> {ok, message, issues, title, text, form(ok일 때)}. 폼 형식은 amount_manual.form_to_rule이 받는 것과 같다."""
    try:
        title, text = (fetch or fetch_page)(url)
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc), "issues": [], "title": "", "text": ""}
    return fill_from_text(service, url, title, text, chat_json)


def fill_from_text(service: dict, url: str, title: str, text: str, chat_json=None) -> dict:
    """이미 읽은(또는 사람이 브라우저에서 복사해 붙여넣은) 본문으로 폼 값을 만든다. 접속이 막힌 사이트에 쓴다.
    근거 구절·숫자는 이 본문과 대조한다. url은 출처 주소로 남는다 (https://…여야 한다)."""
    chat_json = chat_json or __import__("src.generate.condition_extractor", fromlist=["_chat_json"])._chat_json
    text = (text or "").strip()[:MAX_PAGE_CHARS]
    if not url.strip().startswith(("https://", "http://")):
        return {"ok": False, "message": "출처로 남길 자료 주소(https://…)를 입력해 주세요.", "issues": [], "title": title, "text": text}
    if len(text) < 30:
        return {"ok": False, "message": "본문이 너무 짧아요. 자료 본문을 더 붙여넣어 주세요.", "issues": [], "title": title, "text": text}
    user = json.dumps({"제도 이름": service.get("service_name", ""), "수집 데이터 지원내용(참고)": (service.get("support_content") or "")[:600],
                       "페이지 본문": text}, ensure_ascii=False)
    try:
        raw = chat_json(PROMPT, user)
    except Exception as exc:     # noqa: BLE001 — 키 없음·네트워크·JSON 오류를 화면에 알린다
        return {"ok": False, "message": f"AI 호출에 실패했어요: {exc!r}", "issues": [], "title": title, "text": text}
    if raw.get("page_matches_service") is not True:
        return {"ok": False, "message": "이 자료는 선택한 제도와 다른 내용일 수 있어요: " + str(raw.get("match_reason") or "이유 없음"),
                "issues": [], "title": title, "text": text}
    nums, flat, issues = amount_draft.source_numbers(text), amount_draft._squash(text), []
    variants = [c for v in (raw.get("variants") or [])[:MAX_VARIANTS] if isinstance(v, dict) and (c := _clean_variant(v, nums, flat, issues))]
    if not variants:
        return {"ok": False, "message": "자료에서 계산에 쓸 금액·조건을 확인하지 못했어요. 직접 입력해 주세요.", "issues": issues, "title": title, "text": text}
    quotes = "\n".join(dict.fromkeys(v["quote"] for v in variants))
    form = {"variants": [{k: v[k] for k in v if k != "quote"} for v in variants],
            "source": {"title": title or service.get("service_name", ""), "url": url.strip(), "level": default_level(url), "quote": quotes},
            "note": str(raw.get("note") or "").strip()[:200]}
    return {"ok": True, "message": f"유형 {len(variants)}개를 채웠어요.", "issues": issues, "title": title, "text": text, "form": form}
