"""공식 자료 페이지를 읽어 본문 글과 '읽은 기록(스냅샷)'을 남긴다 — 접속이 막히는 원인을 구분해 알려 주고, 우회하지 않는다.

    python -m src.generate.page_fetch check                       # 공식 사이트에 이 PC에서 접속되는지 원인별로 확인
    python -m src.generate.page_fetch fetch URL [URL ...]         # 페이지(HTML·PDF)를 읽어 data/a_validation/official_pages/에 저장
    python -m src.generate.page_fetch fetch URL --browser         # 글이 스크립트로 그려지는 페이지는 Playwright(보통 브라우저)로 렌더링
    python -m src.generate.page_fetch verify [--refresh]          # 저장한 '답변 구성'의 근거 구절이 그 페이지 본문에 정말 있는지 대조

막히는 원인별 처리 (docs/OFFICIAL_PAGE_ACCESS.md):
- 접속 자체가 안 됨(이름 풀이·프록시·시간 초과): 실행 환경의 네트워크 제한이다 -> 허용 목록에 추가하거나 이 PC에서 직접 실행.
- 403·429: 사이트가 자동 접근을 막은 것이다 -> 우회하지 않고, 사람이 브라우저에서 열어 본문을 붙여넣는다.
- 보안문자(CAPTCHA)·로그인: 자동화를 멈추고 사람이 하는 단계로 넘긴다.
- 본문이 비어 있음: 스크립트로 그리는 페이지 -> `--browser` 또는 브라우저에서 본문 복사.
- robots.txt가 막은 경로는 읽지 않는다. 같은 사이트에는 요청 간격(기본 2초)을 둔다.
봇 탐지 회피(위장 헤더·stealth 드라이버 등)는 쓰지 않는다."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import time
import urllib.robotparser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from src.common import config

SNAPSHOT_DIR = config.ROOT / "data" / "a_validation" / "official_pages"      # data/a_validation/ 은 git에 올리지 않는다
USER_AGENT = "Mozilla/5.0 (compatible; GovSupportResearch/1.0)"
MAX_BYTES, TIMEOUT, MIN_INTERVAL, MAX_PDF_PAGES = 5_000_000, 15, 2.0, 80
OFFICIAL_HOSTS = (            # `check`가 확인하고, 클라우드 환경에서는 네트워크 허용 목록에 넣을 도메인
    "www.law.go.kr", "www.moel.go.kr", "minwon.moel.go.kr", "1350.moel.go.kr", "www.work24.go.kr", "m.work24.go.kr", "www.gov.kr",
    "www.korea.kr", "www.bokjiro.go.kr", "www.kamco.or.kr", "www.fsc.go.kr", "www.kinfa.or.kr", "www.kosaf.go.kr", "www.kibo.or.kr",
)
CAPTCHA_HINTS = ("captcha", "보안문자", "자동입력 방지", "자동 입력 방지", "사람인지 확인", "로봇이 아닙니다")
LOGIN_HINTS = ("로그인이 필요", "로그인 후 이용", "로그인 후 확인")

HINTS = {
    "network": "이 실행 환경에서 그 사이트에 접속되지 않아요(네트워크 허용 목록 또는 이 PC의 네트워크 문제). 클라우드 환경이면 환경 설정의 'Network access'에서 도메인을 허용하고, "
               "아니면 이 PC에서 직접 실행하거나 브라우저에서 본문을 복사해 붙여넣어 주세요.",
    "blocked": "사이트가 자동 접근을 막았어요(403·429 등). 우회하지 않아요. 브라우저에서 직접 열어 본문을 복사해 붙여넣어 주세요.",
    "captcha": "보안문자(CAPTCHA)가 나와요. 자동으로 풀지 않아요. 브라우저에서 직접 열어 본문을 복사해 붙여넣어 주세요.",
    "login": "로그인이 필요한 페이지예요. 브라우저에서 로그인해 본문을 복사해 붙여넣어 주세요.",
    "robots": "사이트의 robots.txt가 이 경로의 자동 수집을 허용하지 않아요. 읽지 않아요. 브라우저에서 본문을 복사해 붙여넣어 주세요.",
    "empty": "본문 글이 거의 없어요. 화면을 스크립트로 그리는 페이지일 수 있어요 — --browser 옵션(Playwright)을 쓰거나 브라우저에서 본문을 복사해 붙여넣어 주세요.",
    "too_big": "페이지가 너무 커서 읽지 않았어요.",
    "other": "자료를 읽지 못했어요. 주소가 맞는지 확인해 주세요.",
}


class FetchError(RuntimeError):
    """cause: network | blocked | captcha | login | robots | empty | too_big | other"""

    def __init__(self, cause: str, detail: str = ""):
        self.cause = cause
        super().__init__(f"자료를 열지 못했어요 [{cause}] {detail}".rstrip() + " — " + HINTS[cause])


@dataclass
class Page:
    url: str
    final_url: str
    title: str
    text: str
    status: int
    content_type: str
    fetched_at: str
    method: str            # requests | browser
    sha256: str


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
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except OSError:
        return ""                          # 이름 풀이 실패는 접속 단계에서 원인과 함께 알려 준다
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return "내부망·로컬 주소는 읽을 수 없어요."
    return ""


_last_request: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _wait(host: str, sleep=time.sleep, now=time.monotonic) -> None:
    gap = MIN_INTERVAL - (now() - _last_request.get(host, -1e9))
    if gap > 0:
        sleep(gap)
    _last_request[host] = now()


def robots_allows(url: str, get=None) -> bool:
    """robots.txt가 이 경로의 수집을 허용하는가. robots.txt를 받지 못하면(없음·접속 실패) 허용으로 본다 (접속 실패는 본 요청에서 원인과 함께 드러난다)."""
    import requests
    get = get or requests.get
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = get(base + "/robots.txt", timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200 and "html" not in (resp.headers.get("Content-Type") or "").lower():
                rp.parse(resp.text.splitlines())
                _robots[base] = rp
            else:
                _robots[base] = None
        except requests.RequestException:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def _classify(exc: Exception) -> str:
    import requests
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        return "blocked" if code in (401, 403, 429) else "other"
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return "network"
    return "other"


def _pdf_text(body: bytes) -> tuple[str, str]:
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(body))
    pages = [(p.extract_text() or "").strip() for p in reader.pages[:MAX_PDF_PAGES]]
    meta = getattr(reader, "metadata", None)
    return (str(getattr(meta, "title", "") or ""), "\n\n".join(p for p in pages if p))


def _check_text(raw_html: str, text: str) -> None:
    low = (raw_html + " " + text).lower()
    if len(text) < 800 and any(h in low for h in CAPTCHA_HINTS):
        raise FetchError("captcha")
    if len(text) < 800 and any(h in low for h in LOGIN_HINTS):
        raise FetchError("login")
    if len(text) < 30:
        raise FetchError("empty")


def _render_with_browser(url: str) -> str:
    """보통 브라우저(Playwright, 위장·stealth 없음)로 열어 렌더링된 HTML을 얻는다. 설치돼 있지 않으면 안내한다."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise FetchError("other", "Playwright가 설치돼 있지 않아요 (pip install playwright && playwright install chromium)") from exc
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 2000)
            return page.content()
        finally:
            browser.close()


def fetch(url: str, *, browser: bool = False, get=None, render=None, sleep=time.sleep) -> Page:
    """페이지를 읽는다. 실패하면 원인을 구분한 FetchError. 같은 사이트에는 요청 간격을 두고, robots.txt가 막은 경로는 읽지 않는다."""
    import requests
    problem = check_url(url)
    if problem:
        raise FetchError("other", problem)
    get = get or requests.get
    host = urlparse(url).netloc
    if not robots_allows(url, get):
        raise FetchError("robots")
    _wait(host, sleep)
    fetched_at = datetime.now(timezone.utc).isoformat()
    if browser:
        html = (render or _render_with_browser)(url)
        title, text = html_to_text(html)
        _check_text(html, text)
        return Page(url, url, title, text, 200, "text/html", fetched_at, "browser", hashlib.sha256(html.encode("utf-8")).hexdigest())
    try:
        resp = get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, stream=True)
        resp.raise_for_status()
        body = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        raise FetchError(_classify(exc), type(exc).__name__) from exc
    if len(body) > MAX_BYTES:
        raise FetchError("too_big")
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        title, text = _pdf_text(body)
        raw_html = ""
    else:
        encoding = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else (resp.apparent_encoding or "utf-8")
        raw_html = body.decode(encoding, errors="replace")
        title, text = html_to_text(raw_html)
    _check_text(raw_html, text)
    return Page(url, getattr(resp, "url", url), title, text, resp.status_code, ctype, fetched_at, "requests", hashlib.sha256(body).hexdigest())


# --- 읽은 기록(스냅샷) ---------------------------------------------------------------------------------------------------------
def snapshot_path(url: str, folder: Path | None = None) -> Path:
    return (folder or SNAPSHOT_DIR) / (hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:12] + ".json")


def save_snapshot(page: Page, folder: Path | None = None) -> Path:
    path = snapshot_path(page.url, folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(page), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_snapshot(url: str, folder: Path | None = None) -> Page | None:
    path = snapshot_path(url, folder)
    if not path.is_file():
        return None
    try:
        return Page(**json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError):
        return None


def page_from_text(url: str, title: str, text: str) -> Page:
    """사람이 브라우저에서 복사해 붙여넣은 본문을 읽은 기록 형식으로 만든다 (method='pasted'). 나중에 근거 구절 대조(verify)에 쓴다."""
    return Page(url.strip(), url.strip(), title, text, 0, "text/plain", datetime.now(timezone.utc).isoformat(), "pasted",
                hashlib.sha256(text.encode("utf-8")).hexdigest())


def quote_in_text(text: str, quote: str) -> bool:
    """근거 구절이 본문에 (공백을 무시하고) 글자 그대로 있는가."""
    q = re.sub(r"\s+", "", quote or "")
    return len(q) >= 4 and q in re.sub(r"\s+", "", text or "")


# --- 명령 ---------------------------------------------------------------------------------------------------------------------
def check_hosts(hosts=OFFICIAL_HOSTS, get=None, sleep=time.sleep) -> list[tuple[str, str, str]]:
    """공식 사이트마다 (호스트, 결과, 설명). 결과: ok | network | blocked | other — 원인을 구분해 보여 준다."""
    import requests
    get = get or requests.get
    out = []
    for host in hosts:
        _wait(host, sleep)
        try:
            resp = get(f"https://{host}/", timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, stream=True)
            out.append((host, "ok" if resp.status_code < 400 else ("blocked" if resp.status_code in (401, 403, 429) else "other"), f"HTTP {resp.status_code}"))
        except requests.RequestException as exc:
            out.append((host, _classify(exc), type(exc).__name__))
    return out


def verify_overrides(refresh: bool = False, fetcher=None, folder: Path | None = None) -> list[dict]:
    """저장한 '답변 구성'(AI가 채워 확인 대기 중인 것 포함)의 근거 구절이 그 주소의 본문에 정말 있는지 대조한다.
    결과 상태: ok(본문에 있음) | missing(본문에 없음) | unreachable(읽지 못함, cause) | no_source(금액 안내가 없어 출처 없음)."""
    from src.generate import answer_override
    fetcher = fetcher or fetch
    results = []
    for sid, ov in sorted(answer_override.overrides().items()):
        src = ov.get("source") or {}
        if not src.get("url"):
            results.append({"service_id": sid, "status": "no_source", "url": "", "detail": ""})
            continue
        page = None if refresh else load_snapshot(src["url"], folder)
        if page is None:
            try:
                page = fetcher(src["url"])
                save_snapshot(page, folder)
            except FetchError as exc:
                results.append({"service_id": sid, "status": "unreachable", "url": src["url"], "detail": exc.cause})
                continue
        ok = quote_in_text(page.text, src.get("quote", ""))
        results.append({"service_id": sid, "status": "ok" if ok else "missing", "url": src["url"], "detail": page.fetched_at[:10]})
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="공식 자료 페이지 읽기·접속 확인·근거 구절 대조 (우회하지 않음)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="공식 사이트 접속 가능 여부를 원인별로 확인")
    f = sub.add_parser("fetch", help="페이지를 읽어 저장")
    f.add_argument("urls", nargs="+")
    f.add_argument("--browser", action="store_true", help="Playwright로 렌더링 (스크립트로 그려지는 페이지)")
    f.add_argument("--out", default=str(SNAPSHOT_DIR))
    v = sub.add_parser("verify", help="저장한 답변 구성의 근거 구절을 페이지 본문과 대조")
    v.add_argument("--refresh", action="store_true", help="저장한 기록을 쓰지 않고 다시 읽는다")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        rows = check_hosts()
        for host, status, detail in rows:
            print(f"{'✅' if status == 'ok' else '⛔'} {host:22s} {status:8s} {detail}")
        bad = [r for r in rows if r[1] == "network"]
        if bad:
            print(f"\n{len(bad)}개 사이트에 접속되지 않아요 -> {HINTS['network']}")
        return 0 if all(r[1] == "ok" for r in rows) else 1
    if args.cmd == "fetch":
        code = 0
        for url in args.urls:
            try:
                page = fetch(url, browser=args.browser)
            except FetchError as exc:
                print(f"⛔ {url}\n   {exc}")
                code = 1
                continue
            path = save_snapshot(page, Path(args.out))
            print(f"✅ {url}\n   제목: {page.title or '-'} · {len(page.text):,}자 · {page.method} · 저장: {path}")
        return code
    results = verify_overrides(refresh=args.refresh)
    mark = {"ok": "✅ 본문에 있음", "missing": "❌ 본문에 없음", "unreachable": "⏭ 읽지 못함", "no_source": "- 출처 없음(금액 안내 없음)"}
    for r in results:
        print(f"{mark[r['status']]:18s} {r['service_id']:14s} {r['url']} {r['detail']}")
    return 0 if all(r["status"] in ("ok", "no_source") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
