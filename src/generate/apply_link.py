"""W3: 실제 신청 사이트 링크 (docs/WORK_ITEMS.md W3).

`generator._to_recommendation`이 `Recommendation.apply_url`을 채울 때 호출한다. 찾지 못하면 빈 문자열이다
(없는 링크를 만들지 않는다). 찾는 순서:
1. 사람이 확인한 그 제도의 신청·안내 페이지 (VERIFIED_PAGES, 자주 안내되는 전국 제도)
2. serviceDetail 원본의 '온라인신청사이트URL' (src/ingest/apply_links.py가 만든 대응표)
3. 신청방법 원문에 적힌 주소 ('고용24(www.work24.go.kr) 홈페이지 신청')
2·3은 대부분 사이트 첫 화면이다. 고용24 첫 화면이면 제도 이름 검색 결과로 바꾼다.
활성 제도 10,695개 중 2는 18%, 3은 16%, 둘 중 하나는 22%에 있다 (2026-10-26). 나머지는 카드에 '담당 기관 확인'.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from src.common import config
from src.common.schema import Chunk
from src.generate import citation
from src.ingest import apply_links
from src.ingest import store as ingest_store   # 신청방법 원문 조회 (모듈 속성으로 호출, 테스트에서 바꿔 끼움)

# 'https://…', 'www.…', 'apply.jobaba.net'처럼 쓰인 주소. 한글·공백·괄호·쉼표 앞에서 끊는다.
_URL_RE = re.compile(
    r"(?:https?://|www\.)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?::\d+)?(?:/[^\s<>()\[\]{}'\",가-힣]*)?"
    r"|(?<![A-Za-z0-9.@\-])[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.(?:go|or|ac|co|re|ne|kr)\.kr(?:/[^\s<>()\[\]{}'\",가-힣]*)?"
    r"|(?<![A-Za-z0-9.@\-])[a-z0-9\-]+(?:\.[a-z0-9\-]+)+\.(?:kr|com|net|org)(?:/[^\s<>()\[\]{}'\",가-힣]*)?",
    re.IGNORECASE)
# 원본 필드에는 한글 도메인도 있다 ('http://상생페이백.kr', 'www.청양군청소년재단.com'). 신청방법 원문에서는 한글 주소를 뽑지 않는다
_HOST_RE = re.compile(r"^https?://[A-Za-z0-9가-힣\-]+(?:\.[A-Za-z0-9가-힣\-]+)*\.[A-Za-z]{2,}(?::\d+)?(?:/\S*)?$")


# 그 제도의 신청·안내 페이지를 사람이 확인한 주소 (2026-10-26). 원본 필드는 70%가 사이트 첫 화면이라
# ('국민취업지원제도' -> https://www.work24.go.kr) 사용자가 사이트 안에서 제도를 다시 찾아야 했다.
# 자주 안내되는 전국 제도(평가 질문 답변·정답 빈도 순)만 넣는다. 고용24·국세청·LH·장학재단·서민금융진흥원·
# 주택금융공사·청년몽땅정보통은 페이지를 열어 제도 페이지임을 확인했고, 복지로 상세는 화면을 스크립트로 그려
# 도구로 열 수 없어 검색 색인 제목('복지서비스 상세(중앙) - 기초연금')으로 확인했다. 주소가 바뀌면 여기를 고친다.
VERIFIED_PAGES: dict[str, str] = {
    "149200005007": "https://www.work24.go.kr/ua/z/z/1300/selectEmssRqutIntro.do",      # 국민취업지원제도 (취업지원신청)
    "149200005033": "https://www.work24.go.kr/wk/g/b/1100/busiIntro.do",                # 청년도전지원사업 (온라인 신청하기)
    "149200005032": "https://www.work24.go.kr/hr/a/a/1100/trnnCrsInf2.do",              # K-디지털 트레이닝 (훈련 찾기·신청)
    "149200000026": "https://www.work24.go.kr/hr/h/a/1100/selectIssuGudn.do",           # 국민내일배움카드 (발급안내·신청 바로가기)
    "149200005023": "https://q-net.or.kr/man004.do?ARTL_SEQ=5251124&BOARD_ID=Q001&gId=&gSite=Q&id=man00402",  # 청년 응시료 지원 2026 안내
    "135200005045": "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=WLF00005567",  # 전국민 마음투자
    "508000000565": "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=WLF00001175",  # 자립준비청년 자립수당
    "135200000120": "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=WLF00001171",  # 아동수당
    "SSE000000170": "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=WLF00001164",  # 기초연금
    "134200005012": "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=WLF00003253",  # 가정양육수당
    "134200005036": "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=WLF00003250",  # 영유아보육료
    "SCY000000060": "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=WLF00003235",  # 입양아동 양육수당
    "999000000022": "https://apply.lh.or.kr/lhapply/cm/cntnts/cntntsView.do?mi=1168&cntntsId=1037",  # 기존주택 전세임대
    "999000000021": "https://apply.lh.or.kr/lhapply/cm/cntnts/cntntsView.do?mi=1156&cntntsId=1033",  # 기존주택 매입임대
    "SD0000004508": "https://www.kosaf.go.kr/ko/scholar.do?pg=scholarship05_12_01_01&ttab1=2",       # 국가장학금 Ⅰ유형
    "134200005002": "https://www.kosaf.go.kr/ko/scholar.do?pg=scholarship05_12_01_02",               # 국가장학금 Ⅱ유형
    "999000000090": "https://www.kosaf.go.kr/ko/scholar.do?pg=scholarship05_12_10",                  # 다자녀 국가장학금
    "B55370100009": "https://www.kinfa.or.kr/financialProduct/hessalLoanYoos.do",                    # 햇살론유스
    "B55370100044": "https://www.kinfa.or.kr/financialProduct/youthFutureSavings.do",                # 청년미래적금
    "116010000001": "https://www.hf.go.kr/ko/sub02/sub02_01_06.do",                                  # 주택금융공사 월세자금보증
    "105100000001": "https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=40397&cntntsId=238977",    # 근로·자녀장려금 신청기간 및 방법
    "611000000126": "https://youth.seoul.go.kr/youthConts.do?key=2310100061&sc_bbsCtgrySn=2310200011&sc_pbancSeCd=012",  # 서울 청년수당
    "B55400900031": "https://www.kosmes.or.kr/nsh/SH/SBI/SHSBI004M0.do",   # 혁신창업사업화자금 (융자상담 바로가기)
    "142000000099": "https://www.kosmes.or.kr/nsh/SH/SBI/SHSBI004M0.do",   # 청년전용창업자금 (같은 페이지의 하위 자금)
}
# 신청 양식은 로그인 뒤 사이트 스크립트로 열려 주소가 없다 (고용24 '취업지원신청' 버튼은 href="#none",
# 국가장학금 '통합 신청하기'는 javascript). 그래서 확인한 페이지에서 다음에 누를 것을 카드에 적는다 (2026-10-26
# 페이지를 열어 버튼 이름 확인). 복지로는 페이지를 열 수 없어 아직 넣지 않았다.
_WORK24_RESUME = "https://www.work24.go.kr/wk/a/b/2100/resumeMngMain.do"   # 고용24 구직 등록
NEXT_STEP: dict[str, str] = {
    VERIFIED_PAGES["149200005007"]: "'취업지원신청' 버튼 → 로그인",
    VERIFIED_PAGES["149200005033"]: "'온라인 신청하기' 버튼 → 로그인",
    VERIFIED_PAGES["149200005032"]: "과정을 '검색'해 고른 뒤 그 과정에서 신청",
    VERIFIED_PAGES["149200000026"]: "'국민내일배움카드 발급' 버튼 → 로그인",
    VERIFIED_PAGES["149200005023"]: "공지 아래 첨부 안내문에서 신청 방법 확인",
    VERIFIED_PAGES["999000000022"]: "'신청방법 및 절차' 탭 확인 → 상단 '청약' > '임대주택' > '청약신청'",
    VERIFIED_PAGES["999000000021"]: "'임대절차' 탭 확인 → 상단 '청약' > '임대주택' > '청약신청'",
    VERIFIED_PAGES["SD0000004508"]: "'국가장학금 통합 신청하기' 버튼 → 로그인",
    VERIFIED_PAGES["134200005002"]: "'국가장학금 통합 신청하기' 버튼 → 로그인",
    VERIFIED_PAGES["999000000090"]: "'국가장학금 통합 신청하기' 버튼 → 로그인",
    VERIFIED_PAGES["B55370100009"]: "서민금융진흥원 앱에서 보증 신청 → 협약은행 앱에서 대출",
    VERIFIED_PAGES["B55370100044"]: "모집 기간에 취급 은행 앱에서 가입 신청",
    VERIFIED_PAGES["116010000001"]: "온라인 신청 없음 · 취급은행에서 상담·신청",
    VERIFIED_PAGES["105100000001"]: "홈택스 '장려금·연말정산·전자기부금' → '근로·자녀장려금' → '신청하기'",
    VERIFIED_PAGES["611000000126"]: "상단 '신청하기' → 로그인",
    VERIFIED_PAGES["B55400900031"]: "'융자상담(지역본부) 바로가기' 또는 상단 '사업신청'",
}
# 신청 전에 먼저 해야 하는 일 (페이지에 적힌 것만): (버튼 문구, 주소)
BEFORE: dict[str, tuple[str, str]] = {
    VERIFIED_PAGES["149200005007"]: ("구직 등록 먼저 하기", _WORK24_RESUME),
    VERIFIED_PAGES["149200000026"]: ("일자리가 없으면 구직 등록 먼저", _WORK24_RESUME),
    VERIFIED_PAGES["B55370100009"]: ("금융교육 먼저 이수", "https://www.kinfa.or.kr/financialLife/financialEducation.do"),
}


def next_step(url: str) -> str:
    """확인한 페이지에서 다음에 누를 것 ('취업지원신청' 버튼 → 로그인). 모르면 ''."""
    return NEXT_STEP.get(url, "")


def before(url: str) -> tuple[str, str] | None:
    """신청 전에 먼저 할 일 (버튼 문구, 주소). 없으면 None."""
    return BEFORE.get(url)


# 첫 화면 주소만 있는 고용24 제도는 고용24 통합검색 결과로 보낸다 (제도 이름으로 검색하면 신청 메뉴가 바로 나온다:
# '국민취업지원제도' 검색 -> 신고·신청 탭에 '취업지원신청'. 2026-10-26 확인). 복지로 검색은 주소로 검색어를 넘길 수 없다.
WORK24_SEARCH = "https://www.work24.go.kr/cm/f/c/0100/selectUnifySearch.do?topQuerySearchArea=all&topQueryData="
_WORK24_HOME_RE = re.compile(r"^https?://(?:www\.|m\.)?work24\.go\.kr/?$", re.IGNORECASE)


def find(chunks: list[Chunk], how_to_apply: str) -> str:
    """제도의 실제 신청 사이트 주소 (https://…). 확인한 제도 페이지 -> 원본 필드 -> 신청방법 원문 순서. 없으면 ''.
    사이트 첫 화면이 고용24면 제도 이름 검색 결과 주소로 바꾼다."""
    if not chunks:
        return ""
    sid = chunks[0].service_id
    if sid in VERIFIED_PAGES:
        return VERIFIED_PAGES[sid]
    url = normalize(str((apply_links.load().get(sid) or {}).get("url") or "")) or _url_in_how(sid, chunks, how_to_apply)
    if url and _WORK24_HOME_RE.match(url):
        return WORK24_SEARCH + quote(chunks[0].service_name.strip())
    return url


def kind(url: str) -> str:
    """주소 종류: 'page'(확인한 제도 페이지) / 'search'(사이트 검색 결과) / 'home'(사이트 주소만, 첫 화면일 수 있음) / ''."""
    if not url:
        return ""
    if url in _VERIFIED_URLS:
        return "page"
    if url.startswith(WORK24_SEARCH):
        return "search"
    return "home"


_VERIFIED_URLS = set(VERIFIED_PAGES.values())


def _url_in_how(sid: str, chunks: list[Chunk], how_to_apply: str) -> str:
    for text in _how_texts(sid, chunks, how_to_apply):
        for m in _URL_RE.finditer(text):
            url = normalize(m.group())
            if url:
                return url
    return ""


def normalize(raw: str) -> str:
    """주소를 https://… 꼴로 맞춘다. 주소로 볼 수 없으면 ''. (원본 값: 'www.x.go.kr', 'gb.go.kr', 'https://x/' 등)"""
    url = (raw or "").strip().rstrip(".,;:)]}>'\"")
    if not url or "@" in url or re.search(r"\s", url):
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url if _HOST_RE.match(url) else ""


def _how_texts(sid: str, chunks: list[Chunk], how_to_apply: str) -> list[str]:
    """주소를 찾을 신청방법 글: 근거 조각의 신청방법 원문, 카드의 신청방법(LLM 요약일 수 있음), A 저장소의 원문."""
    texts = [citation._body_text(c) for c in chunks if c.section == "신청방법"]
    texts.append(how_to_apply or "")
    texts.append(_stored_how(sid))
    return [t for t in texts if t]


def _stored_how(sid: str) -> str:
    """A 저장소의 신청방법 원문. DB가 없으면 부르지 않는다 (get_record는 DB 파일이 없으면 새로 만든다)."""
    if not Path(config.DB_PATH).exists():
        return ""
    try:
        record = ingest_store.get_record(sid) or {}
    except Exception:  # DB 잠김·형식 오류 등
        return ""
    return str((record.get("payload") or {}).get("how_to_apply") or "")
