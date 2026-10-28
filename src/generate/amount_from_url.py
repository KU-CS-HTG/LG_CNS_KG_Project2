"""공식 자료 주소(웹페이지)를 읽어 '예상 수령액 규칙 입력 폼'을 미리 채운다 (화면: `python -m app.amount_mode`의 '자료에서 채우기').

사람이 주소만 넣으면 AI가 페이지 본문에서 금액·기간·나이·소득 조건을 찾아 폼 값으로 옮기고, 사람은 세부 내용만 고친다.
AI가 쓴 값은 코드가 페이지 본문과 대조한다: 근거 구절은 본문에 글자 그대로 있어야 하고, 금액·개월 수·비율·한도는 본문에 나온 숫자여야 한다
(없으면 그 유형을 버린다). 나이·중위소득 조건 숫자가 본문에 없으면 조건을 비우고 경고한다. 페이지가 이 제도와 다른 사업이면 채우지 않는다.
저장(규칙 파일에 쓰기)은 여전히 사람이 폼을 확인하고 누른다."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

from src.generate import amount_draft

MAX_PAGE_CHARS, MAX_BYTES, FETCH_TIMEOUT, MAX_VARIANTS = 15000, 3_000_000, 15, 4
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


class _Text(HTMLParser):
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "table", "section", "ul", "dd", "dt"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        elif not self.skip and data.strip():
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """HTML -> (제목, 본문 글). 표 칸은 ' | '로, 줄은 줄바꿈으로 남기고 공백을 정리한다."""
    parser = _Text()
    parser.feed(html or "")
    lines = [re.sub(r"[ \t\xa0]+", " ", l).strip(" |") for l in "".join(parser.parts).splitlines()]
    return parser.title, "\n".join(l for l in lines if l)


def check_url(url: str) -> str:
    """주소 문제 설명(없으면 ''). http(s)만, 내부망·로컬 주소는 막는다."""
    parts = urlparse((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "주소는 https://로 시작해야 해요."
    host = parts.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ""                          # 이름 풀이 실패는 접속 단계에서 알려 준다
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return "내부망·로컬 주소는 읽을 수 없어요."
    return ""


def fetch_page(url: str) -> tuple[str, str]:
    """(제목, 본문 글). 실패하면 RuntimeError(사람이 읽을 수 있는 이유)."""
    import requests
    problem = check_url(url)
    if problem:
        raise RuntimeError(problem)
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}, stream=True)
        resp.raise_for_status()
        body = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        raise RuntimeError(f"자료를 열지 못했어요 ({type(exc).__name__}). 주소가 맞는지, 이 PC에서 접속되는지 확인해 주세요.") from exc
    if len(body) > MAX_BYTES:
        raise RuntimeError("페이지가 너무 커서 읽지 않았어요.")
    encoding = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else (resp.apparent_encoding or "utf-8")
    title, text = html_to_text(body.decode(encoding, errors="replace"))
    if len(text) < 30:
        raise RuntimeError("페이지에서 읽을 글이 거의 없어요. 화면을 스크립트로 그리는 페이지일 수 있어요 — 본문을 직접 복사해 입력해 주세요.")
    return title, text[:MAX_PAGE_CHARS]


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
    chat_json = chat_json or __import__("src.generate.condition_extractor", fromlist=["_chat_json"])._chat_json
    try:
        title, text = (fetch or fetch_page)(url)
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc), "issues": [], "title": "", "text": ""}
    user = json.dumps({"제도 이름": service.get("service_name", ""), "수집 데이터 지원내용(참고)": (service.get("support_content") or "")[:600],
                       "페이지 본문": text}, ensure_ascii=False)
    try:
        raw = chat_json(PROMPT, user)
    except Exception as exc:     # noqa: BLE001 — 키 없음·네트워크·JSON 오류를 화면에 알린다
        return {"ok": False, "message": f"AI 호출에 실패했어요: {exc!r}", "issues": [], "title": title, "text": text}
    if raw.get("page_matches_service") is not True:
        return {"ok": False, "message": "이 페이지는 선택한 제도와 다른 내용일 수 있어요: " + str(raw.get("match_reason") or "이유 없음"),
                "issues": [], "title": title, "text": text}
    nums, flat, issues = amount_draft.source_numbers(text), amount_draft._squash(text), []
    variants = [c for v in (raw.get("variants") or [])[:MAX_VARIANTS] if isinstance(v, dict) and (c := _clean_variant(v, nums, flat, issues))]
    if not variants:
        return {"ok": False, "message": "페이지에서 계산에 쓸 금액·조건을 확인하지 못했어요. 직접 입력해 주세요.", "issues": issues, "title": title, "text": text}
    quotes = "\n".join(dict.fromkeys(v["quote"] for v in variants))
    form = {"variants": [{k: v[k] for k in v if k != "quote"} for v in variants],
            "source": {"title": title or service.get("service_name", ""), "url": url.strip(), "level": default_level(url), "quote": quotes},
            "note": str(raw.get("note") or "").strip()[:200]}
    return {"ok": True, "message": f"유형 {len(variants)}개를 채웠어요.", "issues": issues, "title": title, "text": text, "form": form}
