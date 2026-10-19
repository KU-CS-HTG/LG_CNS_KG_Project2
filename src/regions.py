"""시·도 이름 정규화 — 데이터(소관기관명)와 사용자 입력(질문·사이드바)을 같은 이름으로 맞춘다."""

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

NATIONWIDE = "전국"


def normalize_region(text: str | None) -> str | None:
    """'서울', '서울시', '서울특별시 강남구' → '서울특별시'. 시·도를 못 찾으면 None."""
    if not text:
        return None
    text = text.strip()
    for prefix in _PREFIX_ORDER:
        if text.startswith(prefix):
            return _PREFIX[prefix]
    return None


def region_of_agency(agency: str, agency_type: str = "") -> str:
    """소관기관 → 서비스 지역. 중앙행정기관이거나 시·도를 못 찾으면 '전국'."""
    if agency_type == "중앙행정기관":
        return NATIONWIDE
    return normalize_region(agency) or NATIONWIDE
