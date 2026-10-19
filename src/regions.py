"""지역 이름 정규화 — 데이터(소관기관명)와 사용자 입력(질문·사이드바)을 같은 이름으로 맞춘다.

지역은 두 단계로 다룬다.
- region  : 시·도 (예: 서울특별시) / 중앙부처 제도는 "전국"
- sigungu : 시·군·구 (예: 강남구) / 시·도 전체 또는 전국 제도는 ""
"""
import re

SIDO = [
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
    "대전광역시", "울산광역시", "세종특별자치시", "경기도", "강원특별자치도",
    "충청북도", "충청남도", "전북특별자치도", "전라남도", "경상북도",
    "경상남도", "제주특별자치도",
]

# 접두어 → 정식 명칭. 긴 접두어를 먼저 검사해야 '충청북'이 '충청'보다 우선한다.
_PREFIX = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도",
    "충청북": "충청북도", "충북": "충청북도", "충청남": "충청남도", "충남": "충청남도",
    "전라북": "전북특별자치도", "전북": "전북특별자치도", "전라남": "전라남도", "전남": "전라남도",
    "경상북": "경상북도", "경북": "경상북도", "경상남": "경상남도", "경남": "경상남도",
    "제주": "제주특별자치도",
}
_PREFIX_ORDER = sorted(_PREFIX, key=len, reverse=True)
_SIGUNGU = re.compile(r"[가-힣]+(?:시|군|구)")

NATIONWIDE = "전국"
UNKNOWN_REGION = "지역미상"   # 구·군청 제도인데 어느 시·도인지 기관명에 없는 경우


def normalize_region(text: str | None) -> str | None:
    """'서울', '서울시', '서울특별시 강남구' → '서울특별시'. 시·도를 못 찾으면 None."""
    if not text:
        return None
    text = text.strip()
    for prefix in _PREFIX_ORDER:
        if text.startswith(prefix):
            return _PREFIX[prefix]
    return None


def normalize_sigungu(text: str | None) -> str | None:
    """'노원구', '서울 노원구', '노원구청' → '노원구'. 시·도 이름('서울시')은 시·군·구로 보지 않는다."""
    if not text:
        return None
    for token in reversed(text.split()):          # 마지막 단어가 가장 세부 지역
        token = token.removesuffix("청")           # '노원구청' → '노원구' ('청주시'는 앞이라 영향 없음)
        if _SIGUNGU.fullmatch(token) and normalize_region(token) is None:
            return token
    return None


def sigungu_of_agency(agency: str) -> str:
    """'서울특별시 강남구' → '강남구', '강남구청' → '강남구', '국세청'·'서울특별시' → ''."""
    parts = agency.split()
    if len(parts) >= 2 and normalize_region(parts[0]):
        candidate = parts[1]
    elif len(parts) == 1 and normalize_region(parts[0]) is None:
        candidate = parts[0]
    else:
        return ""
    candidate = candidate.removesuffix("청")
    return candidate if _SIGUNGU.fullmatch(candidate) and normalize_region(candidate) is None else ""


def region_of_agency(agency: str, agency_type: str = "") -> str:
    """소관기관 → 시·도.

    - 기관명에 시·도가 있으면 그 시·도
    - 중앙행정기관이면 '전국'
    - '강남구청'처럼 구·군만 있고 시·도가 없으면 '지역미상' → 지역을 말한 사용자에게는 추천하지 않음
    - 그 외(공공기관 등)는 '전국'
    """
    sido = normalize_region(agency)
    if sido:
        return sido
    if agency_type == "중앙행정기관":
        return NATIONWIDE
    if sigungu_of_agency(agency):
        return UNKNOWN_REGION
    return NATIONWIDE
