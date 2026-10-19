"""질문 -> 정형 조건 추출(LLM). 나이·거주지·상황·관심 분야 (기획서 8.1 질문 개선).

못 찾은 항목은 키를 생략한다 (추측 금지). 표현 차이로 성능이 흔들리면 Query Rewrite / Multi Query를 추가한다.
프롬프트에는 예시 문장을 넣지 않는다 (MinKyu-v1 실험: 예시를 그대로 복사해 서로 다른 질문이 같은 값으로 바뀜).
"""
from __future__ import annotations

import logging
import re
from datetime import date

from src.common.schema import Filters
from src.generate import llm

log = logging.getLogger(__name__)

# 값 목록은 tests/fixtures의 메타데이터 표기에 맞춘 제안. A의 메타데이터 표기가 확정되면 여기만 고친다.
REGIONS = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기",
           "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주")
# 값 -> 뜻. 프롬프트에는 예시 문장 대신 이 정의만 넣는다 (이름만 주면 '전세집 구함'을 전세피해로 오분류함).
FIELD_DEFS = {
    "주거": "월세·전세·주택 구입·이사·주거비",
    "일자리": "취업·실업·구직급여·창업·직업훈련·근로",
    "교육": "학비·장학금·학자금·자격증·교육 프로그램",
    "금융": "대출·이자·자산 형성·저축·채무 조정",
    "생활": "생계비·긴급 지원·공과금 등 생활 안정",
    "보육": "자녀 돌봄·어린이집·양육비",
    "보건의료": "의료비·건강검진·심리 상담",
    "문화": "문화·여가·체육·여행",
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


def extract_conditions(question: str) -> dict:
    """예: {"age": 27, "region": "서울", "situation": ["구직"], "field": "주거"}

    LLM 호출이 실패하면 빈 dict (필터 없이 검색되도록 한다. 서비스가 멈추지 않게)."""
    try:
        raw = llm.chat_json(SYSTEM_PROMPT, question)
    except Exception as e:  # 네트워크·키·JSON 오류
        log.warning("조건 추출 실패, 조건 없이 진행: %s", e)
        return {}
    return normalize_conditions(raw, question=question)


_BIRTH_YEAR_RE = re.compile(r"(?<!\d)(\d{2}|\d{4})\s*년\s*생")


def normalize_conditions(raw: dict, today: date | None = None, question: str | None = None) -> dict:
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

    region = normalize_region(raw.get("region"))
    if region:
        out["region"] = region

    field = raw.get("field")
    if isinstance(field, str) and field.strip() in FIELDS:
        out["field"] = field.strip()

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
    return out


def normalize_region(value) -> str | None:
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


def to_filters(conditions: dict, today: str | None = None) -> Filters:
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

    filters["today"] = today or conditions.get("today") or date.today().isoformat()
    return filters
