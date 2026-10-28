"""판정 기준 문서 근거 (C 담당). 추천 카드의 '확인 필요' 항목에 고시·업무처리기준·사업안내 지침의 조항을 붙인다.

보조금24 API는 '어떤 제도가 있나'만 주고, 소득에 무엇이 들어가는지·따로 사는 부모가 같은 가구인지 같은 판정 세부는
공무원이 보는 판정 기준 문서에 있다 (README 1.2, ARCHITECTURE 3.2). 검색은 B의 guide_store.search_guides,
여기서는 그 결과를 코드로 검증해 Citation(kind="guide")으로 바꾼다.

규칙 (CLAUDE.md 답변 생성 규칙):
- 판정 문서 근거가 있어도 자격을 단정하지 않는다. 근거 문구는 문서 원문의 일부만 옮긴다 (요약·바꿔 쓰기 없음).
- 금액·기한은 보조금24(API) 값이 우선이다. 판정 문서는 해석·설명용이라 화면에 GUIDE_NOTICE를 함께 보인다.
- 사용자 지역과 다른 지역 문서는 근거로 쓰지 않는다. 개정 예고 내용은 '개정 예고'로 구분한다.

속도: 답변을 만들 때는 부르지 않고, 사용자가 카드에서 판정 기준을 볼 때만 카드당 1번 검색한다 (app/main.py).
이어진 질문(answer_guide_question)은 검증을 통과한 조항으로 guide_answer가 자연스러운 답을 쓰고, 화면은 그 아래에
조항 원문을 접어 둔다. 답 쓰기에 실패하거나 코드 검사에 걸리면 고정 안내 문장 + 원문으로 되돌아간다.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import re
import sys

from dataclasses import dataclass, field

from src.common.regions import normalize_region
from src.common.schema import Citation, GuideChunk, Recommendation
from src.generate import condition_extractor, guide_answer

log = logging.getLogger(__name__)

GUIDE_STORE = "src.index.guide_store"   # B의 판정 문서 검색 모듈 (아직 없을 수 있어 이름으로 찾는다)
GUIDE_K = 3                   # 카드 1장에 붙이는 판정 근거 최대 수 (B 계약의 기본 k)
MAX_GUIDE_QUOTE_CHARS = 300   # 화면에 옮기는 근거 문구 최대 길이 (조항 문단이 통째로 붙지 않게)
MIN_KEYWORD_HITS = 2          # 검색어 핵심 낱말이 조각에 이만큼(낱말이 이보다 적으면 모두) 있어야 관련 근거로 본다
GUIDE_NOTICE = ("판정 기준 문서는 자격 해석을 돕는 자료예요. 금액·기한은 위 제도 안내(보조금24) 기준이고, "
                "다르면 담당 기관에 확인해 주세요. 최종 자격은 담당 기관에서 판단해요.")
NO_GUIDE_MESSAGE = "이 조건과 관련된 판정 기준 문서를 찾지 못했어요. 담당 기관에 확인해 주세요."
NO_GUIDE_ANSWER = ("판정 기준 문서에서 이 질문에 대한 내용을 찾을 수 없어요. 담당 기관(주민센터·고용센터 등)에 확인해 주세요. "
                   "다른 제도를 찾으시려면 '비슷한 제도 알려줘'처럼 물어봐 주세요.")
FOUND_GUIDE_ANSWER = "판정 기준 문서에서 질문과 관련된 조항 {n}건을 찾았어요. 해당 여부를 단정하지 않으니 아래 원문을 직접 확인해 주세요."
DRAFT_MARK = "개정 예고"
_DRAFT_RE = re.compile(r"행정\s*예고|개정\s*예고|개정안")

# '확인 필요' 항목에 코드가 붙인 안내 문구 (generator·evidence). 검색어에서 뺀다.
_CODE_NOTE_RE = re.compile(r"\((?:기준은 원문 확인|자료에 기준 금액 없음|소관기관:[^)]*)\)")
_GENERIC_ITEMS = ("자격 요건 충족 여부",)       # 확인할 조건을 못 찾았을 때 넣는 일반 항목: 찾을 조항이 없다
# 조건 문구에 흔한 낱말 (어느 조항에나 나와 관련도 판단에 쓰지 않는다)
_STOPWORDS = {"이하", "이상", "미만", "초과", "이내", "기준", "조건", "여부", "확인", "원문", "해당", "대상", "지원",
              "경우", "포함", "등의", "또는", "그리고", "필요", "자격", "요건",
              "같은", "어떻게", "무엇", "어디", "언제", "얼마", "정말", "제가", "저는", "우리", "중간",   # 후속 질문의 흔한 말
              # 판정 문서 어디에나 나와 관련도를 가르지 못하는 말. '청년'은 넣지 않는다: 청년 특례를 묻는 질문에서는
              # 핵심 낱말이라 빼면 정답 조항(국민취업지원제도 청년 소득 기준)을 놓쳤다
              "동안", "기간", "방법", "신청", "사람"}
# 질문 끝말 ('가구예요' -> '가구', '살면' -> '살') 과 조사
_ENDING_RE = re.compile(r"(?:이에요|예요|인가요|인지|나요|까요|가요|해요|이면|하면|으면|면|요)$")
_JOSA_RE = re.compile(r"(?:으로|에서|에게|까지|부터|이며|이고|을|를|이|가|은|는|의|에|와|과|로|도)$")


def guide_query(service_name: str, items: list[str]) -> str:
    """'확인 필요' 항목들 -> 판정 문서 검색어 1개 (카드당 검색 1번).
    '소득 — 기준 중위소득 48% 이하' -> '주거급여 소득 기준 중위소득 48% 이하'. 코드 안내 문구·일반 항목은 뺀다."""
    parts = [service_name.strip()] if service_name else []
    for item in _clean_items(items):
        parts.append(item.replace(" — ", " "))
    return " ".join(dict.fromkeys(p for p in parts if p))


def find_guides(service_id: str, service_name: str, items: list[str], region: str | None = None,
                k: int = GUIDE_K) -> list[Citation]:
    """카드 1장의 '확인 필요' 항목에 붙일 판정 근거. 검색 1번, 검증을 통과한 것만 Citation(kind="guide")으로.

    - 다른 지역 문서는 버린다 (B도 거르지만 한 번 더: 근거가 틀린 지역이면 안내 자체가 틀린다)
    - 검색어 핵심 낱말이 조각 본문에 실제로 있는 것만 남긴다. 검색은 관련이 없어도 k개를 채워 돌려준다
      (B 검색 점수가 계약에 없어 코드로 확인. 보조금24 검색의 '관련 없음' 판정과 같은 이유)
    - 근거 문구는 조각 원문의 연속된 일부만 (MAX_GUIDE_QUOTE_CHARS 이내)
    - 같은 문서·같은 쪽은 한 번만
    B가 아직 없거나(색인 전) 검색이 실패하면 [] (카드는 판정 근거 없이 그대로 보인다)."""
    cleaned = _clean_items(items)
    if not cleaned:
        return []
    hits = _search(guide_query(service_name, cleaned), [service_id] if service_id else None, region, k)
    return _verified(hits, _keywords(" ".join(cleaned)), region, k)


@dataclass
class GuideAnswer:
    """판정 기준 문서로 답한 후속 질문. UI가 보통 답변(Answer)과 같은 자리에 그리도록 text·no_evidence를 맞춘다."""
    text: str
    citations: list[Citation] = field(default_factory=list)
    no_evidence: bool = False
    interpreted: bool = False      # text가 조항을 읽고 쓴 답이면 True (False면 고정 안내 문장)


def answer_guide_question(question: str, recs: list[Recommendation], region: str | None = None,
                          k: int = GUIDE_K, interpret: bool = False, context: str = "") -> GuideAnswer:
    """후속 질문('부모님과 따로 살면 같은 가구예요?')에 화면에 나온 제도들의 판정 기준 문서 조항으로 답한다.

    검색 1번, find_guides와 같은 검증(지역·관련도·원문 일부·중복). 검증을 통과한 조항만 guide_answer.explain에 줘서
    질문에 맞는 답을 쓰고([1]·[2]는 citations 순서), 코드가 근거 번호·근거 구절·단정 표현을 검사한다. interpret=False
    이거나 답 쓰기에 실패하면 고정 안내 문장(FOUND_GUIDE_ANSWER) + 조항 원문. 앱은 interpret=True, 판정 문서 평가는
    기본 LLM 없이 (--interpret로 켬). context: 사용자가 앞서 한 말 (그 처지에 맞는 기준을 골라 답하게)."""
    ids = list(dict.fromkeys(r.service_id for r in recs if r.service_id))
    query = " ".join([question, *_doc_terms(question), *_search_terms(question)])
    hits = _search(query, ids or None, region, k)
    # 제도 이름을 붙여 한 번 더 찾고 번갈아 합친다: '참여할 수 있나요?'처럼 무엇에 참여하는지 질문에 없으면 이름이
    # 있어야 요건 쪽이 올라오고, 이름만 붙이면 '따로 살면 같은 가구예요?'에서 제도의 일반 쪽이 가구 조항을 밀어냈다
    names = list(dict.fromkeys(_program_name(r.service_name) for r in recs if r.service_id and r.service_name))
    if names:
        hits = _interleave(hits, _search(" ".join([*names, query]), ids or None, region, k))
    pairs = _verified_pairs(hits, _keywords(question), region, k)
    if not pairs:
        return GuideAnswer(text=NO_GUIDE_ANSWER, no_evidence=True)
    cits = [c for c, _ in pairs]
    if interpret:
        names = list(dict.fromkeys(r.service_name for r in recs if r.service_name))
        text = guide_answer.explain(question, names, [(c.service_name, c.page, g.text) for c, g in pairs], context)
        if text == guide_answer.NOT_ANSWERED:      # 조항을 읽어 보니 질문에 답하지 않는다: 엉뚱한 원문을 붙이지 않는다
            return GuideAnswer(text=NO_GUIDE_ANSWER, no_evidence=True)
        if text:
            return GuideAnswer(text=text, citations=cits, interpreted=True)
    return GuideAnswer(text=FOUND_GUIDE_ANSWER.format(n=len(cits)), citations=cits)


def available() -> bool:
    """B의 판정 문서 검색(src.index.guide_store)이 있는가. 없으면 UI가 판정 기준 버튼을 보이지 않는다."""
    if GUIDE_STORE in sys.modules:
        return sys.modules[GUIDE_STORE] is not None
    try:
        return importlib.util.find_spec(GUIDE_STORE) is not None
    except (ImportError, ValueError):
        return False


def question_region(question: str) -> str | None:
    """질문 원문에서 사용자의 시·도 ('서울에 사는' -> '서울'). 판정 문서의 지역 비교용 (LLM 호출 없음)."""
    return condition_extractor._find_region(question or "")


# --- 내부 -----------------------------------------------------------------------------
def _verified(hits: list, keywords: list[str], region: str | None, k: int) -> list[Citation]:
    """검색 결과를 코드로 검증해 Citation(kind="guide")으로: 다른 지역 문서·제도 연혁·관련 없는 조각·같은 문서
    같은 쪽은 뺀다."""
    return [c for c, _ in _verified_pairs(hits, keywords, region, k)]


def _verified_pairs(hits: list, keywords: list[str], region: str | None, k: int) -> list[tuple[Citation, GuideChunk]]:
    """_verified와 같고 조각 원문(GuideChunk)도 함께 (이어진 질문의 답은 인용문보다 넓은 조항 본문을 읽고 쓴다)."""
    out: list[tuple[Citation, GuideChunk]] = []
    seen: set[tuple[str, int]] = set()
    for g in hits:
        if not isinstance(g, GuideChunk) or not _region_ok(g.scope, region) or _is_history(g):
            continue
        quote = _excerpt(g.text, keywords)
        key = (g.doc_id, g.page)
        if not quote or key in seen:
            continue
        seen.add(key)
        out.append((Citation(service_name=_doc_label(g), agency=g.publisher, quote=quote,
                             detail_url=g.source_url, kind="guide", page=g.page), g))
    return out[:k]


def _interleave(first: list, second: list) -> list:
    """두 검색 결과를 번갈아 합친다 (같은 조각은 한 번만, 순서는 first가 먼저)."""
    out, seen = [], set()
    for i in range(max(len(first), len(second))):
        for hits in (first, second):
            g = hits[i] if i < len(hits) else None
            key = getattr(g, "chunk_id", id(g))
            if g is not None and key not in seen:
                seen.add(key)
                out.append(g)
    return out


def _is_history(g: GuideChunk) -> bool:
    """제도 연혁(지침 개정 이력표) 조각인가. 지난 해 금액·기준('근로소득공제 20만원+30% → 40만원+30%', 2018년)이
    적혀 있어 지금 기준처럼 보이면 안 된다. 2026년 국민기초생활보장사업 안내 9장 연혁은 쪽마다 표 제목
    '일 시 | 추 진 내 용'이 있다. 법령 쪽의 '[법률 …, 일부개정]'은 현재 법령 표시라 거르지 않는다."""
    return "연혁" in (g.heading or "") or "일시추진내용" in _squash(g.text)


def _search(query: str, service_ids: list[str] | None, region: str | None, k: int) -> list:
    """B의 search_guides (모듈 속성으로 호출: 테스트에서 가짜로 바꿔 끼운다). 모듈이 없거나 실패하면 []."""
    try:
        guide_store = importlib.import_module(GUIDE_STORE)
    except ImportError:                 # B 미구현
        return []
    try:
        return list(guide_store.search_guides(query, service_ids=service_ids, region=region, k=k) or [])
    except Exception as e:  # 색인 없음·모델 오류 등: 카드는 그대로 보인다
        log.warning("판정 기준 문서 검색 실패: %s", e)
        return []


def _clean_items(items: list[str] | None) -> list[str]:
    out = []
    for item in items or []:
        text = " ".join(_CODE_NOTE_RE.sub("", item or "").split())
        if text and text not in _GENERIC_ITEMS and text not in out:
            out.append(text)
    return out


def _keywords(text: str) -> list[str]:
    """관련도 확인용 핵심 낱말 (2자 이상, 흔한 조건 낱말 제외). Kiwi(형태소 분석)가 있으면 명사만, 없으면 정규식.

    정규식만 쓰면 질문의 동사·부사가 섞였다 ('살인데', '받을', '돈도', '계산되': 실제 PDF로 점검한 판정 문서
    평가 질문 12개 중 3개에서 근거 문구를 엉뚱한 줄 중심으로 잘랐다). Kiwi는 requirements.txt에 없어
    (팀 합의 전) 설치된 PC에서만 쓰고, 없으면 예전 방식으로 동작한다."""
    nouns = _kiwi_nouns(text)
    words = (list(dict.fromkeys(w for w in nouns if len(w) >= 2 and w not in _STOPWORDS))
             if nouns is not None else _regex_keywords(text))
    return list(dict.fromkeys(words + [_squash(t) for t in _doc_terms(text)] + _ages(text)))


# 질문 말 -> 판정 문서 말 (질문에 그 표현이 있을 때만 검색어·핵심 낱말에 덧붙인다. 문서 말은 PDF에 있는 것만).
# '일한 경험'은 Kiwi가 '일'(한 글자)을 버려 '경험'만 남고, 문서는 '취업 경험'이라 정답 쪽(국민취업지원제도
# 업무매뉴얼 Ⅰ유형 요건)이 검색에서 밀렸다. '취업' 한 낱말은 매뉴얼 거의 모든 쪽에 있어 붙은 말 그대로 쓴다
_DOC_TERMS = (
    (re.compile(r"일한\s*(?:경험|적)|일해\s*본"), "취업경험"),
    (re.compile(r"따로\s*(?:살|사)"), "주거를 달리"),
    (re.compile(r"아르바이트|알바"), "근로소득"),
    # '제가 버는 돈이 많으면 부모님이랑 가구를 나눌 수 있나요?': 핵심 낱말이 '부모'·'가구'뿐이라 별도가구 사용대차
    # 사례(130쪽)가 올라왔고, LLM이 그걸 '소득이 많으면 별도가구로 인정'으로 넓혀 썼다 (정답: 188쪽·기초 16쪽 가구 분리)
    (re.compile(r"버는\s*돈|번\s*돈|수입|월급|돈을?\s*벌"), "소득"),
    (re.compile(r"가구를?\s*(?:나누|나눌|나눠|분리|따로)|가구\s*분리"), "가구분리"),
    # '중간에 취업하면 어떻게 돼요?': 매뉴얼은 '취업지원 종료'(주 30시간 미만 불완전 취업자는 계속 참여, 114쪽)로 적는다
    (re.compile(r"(?:취업|취직)(?:하면|했|을\s*하면|이\s*되면)|(?:일자리를?|일을?)\s*구하면"), "종료"),
    # '중간에 일을 구하면 생계급여가 끊겨요?': 기초생활보장 사업안내는 '종료'가 아니라 '급여중지'(변동사항을 반영해
    # 급여중지·급여변경, 30·43쪽)로 적어, '종료'만 붙이면 관련 쪽이 검증을 통과하지 못해 '찾을 수 없어요'가 나왔다
    (re.compile(r"끊기|끊겨|끊어|끊길|중단|못\s*받게|안\s*나오"), "급여중지"),
)


def _doc_terms(text: str) -> list[str]:
    return [term for pattern, term in _DOC_TERMS if pattern.search(text or "")]


# 검색어에만 덧붙이는 문서 말 (관련도 확인 낱말에는 넣지 않는다). '일한 경험이 없어도 참여할 수 있나요?'는 '취업경험'만
# 붙이면 매뉴얼의 '일경험 프로그램'(인턴 사업) 쪽이 올라오고 참여 요건 쪽(10·23쪽)이 밀렸다. '받을 수 있나요'처럼
# 흔한 말에 붙이면 '전세사기 피해 … 보상받을 수 있나요?'(근거 없음 문항)에 엉뚱한 조항이 붙어 넣지 않았다
_SEARCH_TERMS = (
    (re.compile(r"일한\s*(?:경험|적)|일해\s*본"), "요건 지원대상"),
    # 취업하면 소득이 바뀐다: 기초생활보장은 '소득 변동'을 확인해 '급여변경'·'급여중지'에 반영한다
    (re.compile(r"(?:취업|취직)(?:하면|했|을\s*하면|이\s*되면)|(?:일자리를?|일을?)\s*구하면"), "소득 변동 급여변경"),
)


def _search_terms(text: str) -> list[str]:
    return [term for pattern, term in _SEARCH_TERMS if pattern.search(text or "")]


def _program_name(name: str) -> str:
    """검색어에 붙일 제도 이름 ('주거급여 (맞춤형 급여)' -> '주거급여')."""
    return re.sub(r"\s*\([^)]*\)", "", name or "").strip()


# 질문의 나이 ('30살', '25 살', '19세') -> 판정 문서의 표기 '30세'. 숫자는 명사가 아니라 빠졌고
# '살'과 '세'가 달라 나이 기준 조항('30세 미만의 미혼자녀')이 관련도 확인에서 걸리지 않았다
_AGE_RE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:살|세)")
_AGE_WORD_RE = re.compile(r"\d{1,3}세")


def _ages(text: str) -> list[str]:
    return [f"{n}세" for n in _AGE_RE.findall(text)]


_KIWI_NOUN_TAGS = ("NNG", "NNP", "SL", "XR")   # 일반·고유 명사, 외국어, 어근
# 제도 이름처럼 끝나는 말 (generator._named_programs와 같은 끝말 + 급여). Kiwi가 '청년도약계좌'를
# '청년 + 도 + 약계좌'로 쪼개 제도 이름이 핵심 낱말에서 사라졌다 -> 분석 전에 사용자 사전에 고유명사로 넣는다
_PROGRAM_WORD_RE = re.compile(r"[가-힣]{2,}?(?:적금|통장|계좌|공제|수당|장려금|바우처|장학금|패스|급여)")
_kiwi = None


def _kiwi_nouns(text: str) -> list[str] | None:
    """Kiwi로 뽑은 명사. 띄어 쓰지 않고 이어진 명사는 이어 붙인다 ('주거급여' -> 주거+급여 -> '주거급여').
    Kiwi가 없거나 실패하면 None (정규식으로 대신)."""
    global _kiwi
    try:
        if _kiwi is None:
            from kiwipiepy import Kiwi
            _kiwi = Kiwi()
        for name in set(_PROGRAM_WORD_RE.findall(text)):
            _kiwi.add_user_word(name, "NNP")      # 이미 있으면 그대로 (False)
        tokens = _kiwi.tokenize(text)
    except Exception:  # 미설치(ImportError)·모델 오류
        return None
    out, cur, end = [], "", None
    for t in tokens:
        if t.tag not in _KIWI_NOUN_TAGS:
            if cur:
                out.append(cur)
            cur, end = "", None
            continue
        if cur and t.start == end:        # 바로 앞 명사에 붙어 있다
            cur += t.form
        else:
            if cur:
                out.append(cur)
            cur = t.form
        end = t.start + t.len
    if cur:
        out.append(cur)
    return out


def _regex_keywords(text: str) -> list[str]:
    """정규식 방식 (Kiwi가 없을 때): 2자 이상 한글 낱말에서 조사·질문 끝말을 뗀다."""
    def strip(w: str) -> str:
        cut = _ENDING_RE.sub("", w)
        if cut != w and len(cut) < 2:
            return ""                      # 어미를 떼면 한 글자인 동사 ('살면', '받으면'): 핵심 낱말이 아니다
        w = cut
        cut = _JOSA_RE.sub("", w)
        return cut if len(cut) >= 2 else w  # 떼고 2자 이상 남을 때만 ('따로'의 '로'는 조사가 아님)
    words = (strip(w) for w in re.findall(r"[가-힣]+", text))
    return list(dict.fromkeys(w for w in words if len(w) >= 2 and w not in _STOPWORDS))


def _has(word: str, body: str) -> bool:
    """낱말이 본문(공백 제거)에 있는가. 3자 이상이면 끝 글자를 뗀 형태도 본다 ('부모님' -> 원문의 '부모')."""
    return word in body or (len(word) >= 3 and word[:-1] in body)


def _region_ok(scope: str | None, region: str | None) -> bool:
    """전국 문서는 항상, 지역 문서는 사용자 지역과 같을 때만. 지역을 알 수 없는 문서는 쓰지 않는다."""
    if not scope or scope.strip() == "전국":
        return True
    doc_region = normalize_region(scope)
    return doc_region is not None and doc_region == normalize_region(region or "")


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _excerpt(text: str, keywords: list[str]) -> str:
    """조각에서 핵심 낱말이 가장 많이 든 줄을 중심으로 MAX_GUIDE_QUOTE_CHARS 이내의 연속된 원문을 자른다.
    핵심 낱말이 MIN_KEYWORD_HITS개(낱말 수가 더 적으면 전부)보다 적게 들어 있으면 관련 없는 조각으로 보고 ''.
    나이('30세')는 자를 줄을 고를 때만 쓰고 관련도 기준에는 세지 않는다 (문서 곳곳에 나와 '가구'+'30세'만
    맞은 재산 차감 쪽이 걸렸다). 낱말이 나이뿐이면 나이로 센다."""
    body = _squash(text)
    found = [w for w in keywords if _has(w, body)]
    topic = [w for w in keywords if not _AGE_WORD_RE.fullmatch(w)] or keywords
    topic_found = [w for w in found if w in topic]
    if not keywords or len(topic_found) < min(MIN_KEYWORD_HITS, len(topic)):
        return ""
    lines = [m for m in re.finditer(r"[^\n]+", text) if m.group().strip()]
    if not lines:
        return ""
    score = lambda m: sum(1 for w in found if _has(w, _squash(m.group())))  # noqa: E731
    best = max(range(len(lines)), key=lambda i: (score(lines[i]), -i))
    start, end = lines[best].start(), lines[best].end()
    if end - start > MAX_GUIDE_QUOTE_CHARS:      # 한 줄이 길면 첫 핵심 낱말 앞쪽부터 자른다
        line = text[start:end]
        first = min((line.find(w[:2]) for w in found if w[:2] in line), default=0)
        start = start + max(0, first - MAX_GUIDE_QUOTE_CHARS // 3)
        return text[start:start + MAX_GUIDE_QUOTE_CHARS].strip()
    lo, hi = best, best                           # 앞뒤 줄을 번갈아 붙여 문맥을 넓힌다
    while True:
        grew = False
        for j in (hi + 1, lo - 1):
            if 0 <= j < len(lines):
                s, e = min(start, lines[j].start()), max(end, lines[j].end())
                if e - s <= MAX_GUIDE_QUOTE_CHARS:
                    start, end, grew = s, e, True
                    lo, hi = min(lo, j), max(hi, j)
        if not grew:
            break
    return text[start:end].strip()


def _doc_label(g: GuideChunk) -> str:
    """화면에 보일 문서 이름: 연도가 문서명에 없으면 붙이고, 개정 예고 조각이면 표시한다."""
    label = g.doc_title.strip()
    if g.year and str(g.year) not in label:
        label = f"{label} ({g.year}년)"
    if _DRAFT_RE.search(f"{g.heading} {g.text}"):
        label = f"{label} · {DRAFT_MARK}"
    return label
