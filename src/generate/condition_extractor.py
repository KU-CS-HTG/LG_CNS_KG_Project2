"""질문 -> 정형 조건 추출(LLM). 나이·거주지·상황·관심 분야 (기획서 8.1 질문 개선).

못 찾은 항목은 키를 생략한다 (추측 금지). 표현 차이로 성능이 흔들리면 Query Rewrite / Multi Query를 추가한다.
프롬프트에는 예시 문장을 넣지 않는다 (MinKyu-v1 실험: 예시를 그대로 복사해 서로 다른 질문이 같은 값으로 바뀜).
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from src.common import config
from src.common.schema import Filters
from src.generate import llm_cache

log = logging.getLogger(__name__)

# 지역 값은 tests/fixtures의 표기에 맞춘 제안 (실제 데이터에는 아직 지역 메타데이터가 없다. A 문서 INGEST_A.md).
REGIONS = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기",
           "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주")
# 분야 값 = A가 넣는 보조금24 '서비스분야' 원문 10가지 (2026-10-20 실제 수집 확인). 값 -> 뜻.
# 프롬프트에는 예시 문장 대신 이 정의만 넣는다 (이름만 주면 '전세집 구함'을 전세피해로 오분류함).
# 실제 분류는 들쭉날쭉하다(청년월세가 '생활안정'). 분야는 B의 가산점일 뿐이라 거르지는 않는다.
FIELD_DEFS = {
    "고용·창업": "취업·구직·면접·직업훈련·인턴·창업·근로",
    "주거·자립": "월세·전세·보증금·임대주택·주택 구입·주거비·자립",
    "보육·교육": "학비·등록금·장학금·학자금·교육 프로그램·자녀 돌봄·어린이집",
    "생활안정": "생계비·긴급 지원·교통비·자산 형성·저축·채무 등 생활 안정",
    "보건·의료": "의료비·건강검진·정신건강·심리 상담",
    "보호·돌봄": "돌봄 서비스·보호 시설·자립준비청년 보호",
    "임신·출산": "임신·출산·결혼·다자녀",
    "문화·환경": "문화·여가·체육·여행·동아리·환경",
    "농림축산어업": "농업·어업·임업·귀농·귀어",
    "행정·안전": "상해보험·안전 장비 등 안전",
}
SITUATION_DEFS = {
    "구직": "일자리를 찾고 있거나 실직·퇴사한 상태",
    "재직": "회사 등에 다니며 일하는 중",
    "창업": "창업했거나 창업을 준비 중",
    "학생": "대학생·대학원생 등 재학 중",
    "신혼": "결혼했거나 결혼을 앞둠",
    "출산": "임신 중이거나 출산함",
    "양육": "자녀를 키우는 중",
    "무주택": "본인 소유 집이 없음",
    "저소득": "소득이 적거나 생활이 어렵다고 밝힘",
    "장애": "본인이 장애가 있음",
    "한부모": "혼자 자녀를 키움",
    "전세사기피해": "전세사기·보증금 미반환 피해를 실제로 당함",
    "채무": "빚·대출 상환이 부담됨",
}
FIELDS = tuple(FIELD_DEFS)
SITUATIONS = tuple(SITUATION_DEFS)
# '주거'·'보건의료'처럼 짧게 쓴 값 -> 실제 분야명 (가운뎃점 앞뒤 낱말, 가운뎃점을 뺀 이름)
_FIELD_ALIASES = {**{part: name for name in FIELDS for part in name.split("·")},
                  **{name.replace("·", ""): name for name in FIELDS}}

# 정식 명칭·옛 명칭 -> 짧은 이름
_REGION_ALIASES = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
    "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "강원도": "강원", "강원특별자치도": "강원", "충청북도": "충북", "충청남도": "충남",
    "전라북도": "전북", "전북특별자치도": "전북", "전라남도": "전남", "경상북도": "경북", "경상남도": "경남",
    "제주도": "제주", "제주특별자치도": "제주",
}

def _defs(d: dict[str, str]) -> str:
    return "\n".join(f"  - {k}: {v}" for k, v in d.items())


SYSTEM_PROMPT = f"""너는 정부 지원제도 검색을 위해 사용자 질문에서 사용자 본인의 조건을 뽑는 도구다.
JSON 객체 하나만 출력한다. 사용할 수 있는 키는 아래뿐이다.

- age: 사용자 본인의 나이(정수). 질문에 나이가 숫자로 직접 나올 때만 쓴다. 나이대처럼 범위로만 말하면 쓰지 않는다.
- birth_year: 사용자 본인의 출생연도(4자리 정수). 출생연도만 나오면 age는 계산하지 말고 birth_year만 쓴다.
- region: 사용자가 사는 광역 시·도. 반드시 다음 중 하나: {", ".join(REGIONS)}.
  시·군·구만 나오면 그것이 속한 시·도가 하나로 분명할 때만 쓴다.
- situation: 질문에 드러난 사용자의 상황. 아래 목록의 이름만 고른 배열 (뜻에 맞을 때만):
{_defs(SITUATION_DEFS)}
- field: 사용자가 가장 도움받고 싶은 분야 하나. 아래 목록의 이름 중 하나:
{_defs(FIELD_DEFS)}

규칙:
- 질문에 근거가 없는 키는 아예 넣지 않는다. 추측하지 않는다.
- 자녀·부모 등 가족의 나이나 거주지를 사용자 본인의 값으로 쓰지 않는다.
- 목록에 없는 값은 쓰지 않는다. 해당하는 값이 없으면 그 키를 뺀다.
- 조건이 하나도 없으면 빈 객체를 출력한다."""

# 질문이 찾는 것(원하는 도움·부담·제도 이름)은 따로 짧게 묻는다. 위 프롬프트에 help 키로 함께 넣었더니
# 나이·지역 같은 조건이 없는 질문('기초연금은 어떻게 신청하나요?')에서 빈 객체만 돌려줬다 (실제 데이터 평가 q33·q34).
HELP_PROMPT = """너는 정부 지원제도 질문에서 사용자가 찾는 것을 가리키는 구절만 뽑는 도구다.
JSON 객체 하나만 출력한다: {"asked": [구절, ...]}

- asked: 사용자가 받고 싶은 도움, 덜고 싶은 부담, 또는 묻는 제도 이름을 질문 원문에서 글자 그대로 복사한 짧은 구절.
- 나이·사는 곳·신분·처지(취업준비생, 직장인, 대학생 등)는 넣지 않는다.
- '지원', '혜택', '제도'처럼 무엇인지 특정하지 않는 말만 있으면 빈 배열이다."""
# 구절 끝의 질문·요청 어미 ('응시료를 지원받을 수 있나요' -> '응시료'). 이유 문장에 그대로 들어가 어색했다
# LLM이 '있을까요' 앞에서 잘라 '주거비를 지원받을 수'처럼 '받을 수'만 남기기도 해서 '있…' 부분은 없어도 지운다
_QUESTION_TAIL_RE = re.compile(
    r"\s*(?:을|를|이|가|은|는)?\s*(?:(?:지원\s*)?받을\s*수(?:\s*있(?:나요|을까요|는지|어요|습니까))?|"
    r"있나요|있을까요|있는지|알려\s*주세요|알려\s*줘|궁금해요|어떻게\s*되나요)\??$")
# 무엇인지 특정하지 않는 말.구절에서 이것들을 빼고 남는 것이 없으면 넓은 질문으로 본다 ('청년 지원', '받을 수 있는 지원')
_GENERIC_HELP = ("받을수있는", "청년", "지원금", "지원", "혜택", "제도", "정책", "신청", "정부", "서비스")
# 사용자가 누구인지(가구·신분)만 가리키는 구절. 프롬프트로 빼라고 해도 '다자녀가구'를 찾는 것으로 돌려줘
# '다자녀가구에 도움이 될 수 있는 제도'로 다자녀 '우대' 통계조사원 채용이 1·2번 카드였다 ('서울 중구 다자녀가구 혜택').
# 처지는 situation(양육 등)으로 따로 잡힌다. '이자'·'보증금'처럼 도움을 뜻하는 말과 겹치지 않는 끝말만 쓴다.
_STATUS_ONLY_RE = re.compile(r"(?:가구|가정|가족|세대|부부|계층|저소득층|수급자|준비생|대학생|학생|직장인|어르신|노인|"
                             r"장애인|임산부|구직자|실직자|청년)$")


# W1: 이미 이용 중인 제도. 질문에 이용 중임을 뜻하는 말이 있을 때만 세 번째 LLM 호출을 한다 (대부분의 질문은 호출이 늘지 않는다).
_IN_USE_HINT_RE = re.compile(r"듣고|듣는|받고|받는\s*중|이용|참여|수강|수료|다니|하고\s*있|하는\s*중|가입|수급|신청(?:했|해\s*둔|을\s*했)|이미|중인|중이")
IN_USE_PROMPT = """너는 질문에서 사용자가 이미 이용·참여·수강·수급 중이거나 이미 신청해 둔 정부 지원제도·프로그램 이름만 뽑는 도구다.
JSON 객체 하나만 출력한다: {"in_use": [이름, ...]}

- in_use: 질문 원문에서 글자 그대로 복사한 제도·프로그램 이름 (예: 질문에 적힌 영어 이름이면 영어 그대로).
- 앞으로 받고 싶은 것, 묻고 있는 것, 사용자의 신분·처지(취업준비생 등)는 넣지 않는다.
- 이미 이용 중인 제도 이름이 질문에 없으면 빈 배열이다."""
MAX_IN_USE = 5
MAX_IN_USE_CHARS = 30

_client = None


def _chat_json(system: str, user: str) -> dict:
    """gpt-4o-mini를 JSON 모드로 호출해 dict를 돌려준다. 클라이언트는 처음 쓸 때 만든다.

    같은 입력의 응답은 llm_cache에 저장해 다시 쓴다 (같은 질문이면 같은 답)."""
    global _client
    cached = llm_cache.get(config.GENERATION_MODEL, system, user)
    if cached is not None:
        return cached
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        from openai import OpenAI
        _client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=30)
    resp = _client.chat.completions.create(
        model=config.GENERATION_MODEL,
        temperature=0,
        seed=llm_cache.SEED,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"JSON 객체가 아닌 응답: {data!r}")
    llm_cache.put(config.GENERATION_MODEL, system, user, data)
    return data


def extract_conditions(question: str) -> dict:
    """예: {"age": 27, "region": "서울", "situation": ["구직"], "field": "주거·자립"}

    LLM 호출이 실패하면 빈 dict (필터 없이 검색되도록 한다. 서비스가 멈추지 않게).
    조건 호출과 찾는 것(help) 호출은 서로 관계없어 동시에 보낸다 (차례로 하면 약 2초, 동시에 하면 약 1초)."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        cond = pool.submit(_chat_json, SYSTEM_PROMPT, question)
        asked = pool.submit(_chat_json, HELP_PROMPT, question)
        in_use = pool.submit(_chat_json, IN_USE_PROMPT, question) if _IN_USE_HINT_RE.search(question) else None
    try:
        raw = cond.result()
    except Exception as e:  # 네트워크·키·JSON 오류
        log.warning("조건 추출 실패, 조건 없이 진행: %s", e)
        return {}
    try:
        raw["help"] = asked.result().get("asked")
    except Exception as e:  # 찾는 것을 못 뽑으면 넓은 질문처럼 거르지 않는다
        log.warning("찾는 것 추출 실패, 없이 진행: %s", e)
    if in_use is not None:
        try:
            raw["in_use"] = in_use.result().get("in_use")
        except Exception as e:  # 이용 중인 제도를 못 뽑으면 거르지 않고 진행 (추천을 빼는 쪽이 아니라 두는 쪽이 안전)
            log.warning("이용 중인 제도 추출 실패, 없이 진행: %s", e)
    return _normalize_conditions(raw, question=question)


_BIRTH_YEAR_RE = re.compile(r"(?<!\d)(\d{2}|\d{4})\s*년\s*생")


def _normalize_conditions(raw: dict, today: date | None = None, question: str | None = None) -> dict:
    """LLM 출력을 정해진 키·값 목록으로 정리한다. 목록 밖의 값이나 형식이 틀린 값은 버린다.

    question을 주면 나이를 질문 원문과 대조한다 (LLM이 '20대 후반'을 28로 추측하거나 출생연도로 직접 계산해
    틀리는 일을 막는다. 나이는 B의 연령 필터에 쓰이므로 틀리면 제도가 통째로 빠진다)."""
    out: dict = {}
    raw = dict(raw)
    if question is not None:
        m = _BIRTH_YEAR_RE.search(question)
        if m:
            yy = int(m.group(1))
            if yy < 100:
                cur = (today or date.today()).year % 100
                yy += 2000 if yy <= cur else 1900
            raw["birth_year"], raw["age"] = yy, None
        elif _to_int(raw.get("age")) is not None and not re.search(rf"(?<!\d){_to_int(raw['age'])}(?!\d)", question):
            raw["age"] = None

    age = _to_int(raw.get("age"))
    if age is None:
        birth_year = _to_int(raw.get("birth_year"))
        if birth_year and 1900 < birth_year <= (today or date.today()).year:
            age = (today or date.today()).year - birth_year   # 생일 전이면 1살 많게 나올 수 있다
    if age is not None and 0 <= age <= 120:
        out["age"] = age

    region = _normalize_region(raw.get("region"))
    if region:
        out["region"] = region

    field = raw.get("field")
    if isinstance(field, str):
        field = field.strip().replace(" ", "")
        field = field if field in FIELDS else _FIELD_ALIASES.get(field)
        if field:
            out["field"] = field

    situation = raw.get("situation")
    if isinstance(situation, str):
        situation = [situation]
    if isinstance(situation, list):
        picked = []
        for s in situation:
            if isinstance(s, str) and s.strip() in SITUATIONS and s.strip() not in picked:
                picked.append(s.strip())
        if picked:
            out["situation"] = picked

    # help: 질문이 찾는 것 (C 내부용. B에게 넘기는 Filters에는 들어가지 않는다).
    # 낱말이 모두 질문 원문에 있는 짧은 구절만 받는다 (나이와 같은 원리로 추측 차단. LLM이 조사를 조금 바꿔 쓰는 것은 허용)
    help_ = raw.get("help")
    if isinstance(help_, str):
        help_ = [help_]
    if isinstance(help_, list) and question is not None:
        q = "".join(question.split())
        picked = []
        for h in help_:
            h = _QUESTION_TAIL_RE.sub("", h.strip()) if isinstance(h, str) else ""
            words, rest = h.split(), "".join(h.split())
            for g in _GENERIC_HELP:
                rest = rest.replace(g, "")
            if (words and len("".join(words)) <= 25 and all(w in q for w in words)
                    and len(rest) >= 2 and h not in picked and not _STATUS_ONLY_RE.search("".join(words))):
                picked.append(h)
        if picked:
            out["help"] = picked
    # in_use: 이미 이용 중인 제도 이름 (C 내부용, 필터에 들어가지 않는다). help와 같은 원리로 질문 원문에 있는 낱말만 받는다
    in_use = raw.get("in_use")
    if isinstance(in_use, str):
        in_use = [in_use]
    if isinstance(in_use, list) and question is not None:
        q = "".join(question.split())
        picked = []
        for name in in_use:
            name = name.strip() if isinstance(name, str) else ""
            words = name.split()
            if (words and len("".join(words)) <= MAX_IN_USE_CHARS and all(w in q for w in words) and name not in picked):
                picked.append(name)
        if picked:
            out["in_use"] = picked[:MAX_IN_USE]
    return out


def _normalize_region(value) -> str | None:
    """'서울특별시'·'서울시'·'서울' -> '서울'. 목록에 없으면 None."""
    if not isinstance(value, str):
        return None
    v = value.strip().replace(" ", "")
    if v in REGIONS:
        return v
    if v in _REGION_ALIASES:
        return _REGION_ALIASES[v]
    if v.endswith(("시", "도")) and v[:-1] in REGIONS:
        return v[:-1]
    return None


def _find_region(text: str) -> str | None:
    """구절에 처음 나오는 시·도의 짧은 이름 ('서울에 사는' -> '서울', '대전광역시에 주민등록' -> '대전'). 없으면 None."""
    found = [(text.find(name), short) for name, short in
             [*((r, r) for r in REGIONS), *_REGION_ALIASES.items()] if name in text]
    return min(found)[1] if found else None


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def to_filters(conditions: dict) -> Filters:
    """추출된 조건을 B의 검색 필터로 변환. 값이 비었거나 형식이 맞지 않는 항목은 키를 생략한다."""
    filters: Filters = {}

    age = conditions.get("age")
    if isinstance(age, str) and age.strip().isdigit():
        age = int(age)
    if isinstance(age, int) and not isinstance(age, bool) and 0 <= age <= 120:
        filters["age"] = age

    for key in ("region", "field"):
        value = conditions.get(key)
        if isinstance(value, str) and value.strip():
            filters[key] = value.strip()

    situation = conditions.get("situation")
    if isinstance(situation, str):
        situation = [situation]
    if isinstance(situation, list):
        situation = [s.strip() for s in situation if isinstance(s, str) and s.strip()]
        if situation:
            filters["situation"] = situation

    filters["today"] = date.today().isoformat()   # 신청기한이 지난 제도 제외 기준일
    return filters
