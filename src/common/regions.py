"""Canonical retrieval regions shared by A/B; normalization is not eligibility inference."""
import re

REGION_ALIASES = {
    "서울특별시": "서울", "서울시": "서울", "부산광역시": "부산", "부산시": "부산",
    "대구광역시": "대구", "대구시": "대구", "인천광역시": "인천", "인천시": "인천",
    "광주광역시": "광주", "광주시": "광주", "대전광역시": "대전", "대전시": "대전",
    "울산광역시": "울산", "울산시": "울산", "세종특별자치시": "세종", "세종시": "세종",
    "경기도": "경기", "강원도": "강원", "강원특별자치도": "강원",
    "충청북도": "충북", "충청남도": "충남", "전라북도": "전북", "전북특별자치도": "전북",
    "전라남도": "전남", "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주", "제주도": "제주",
}
CANONICAL_REGIONS = tuple(dict.fromkeys(REGION_ALIASES.values()))
REGION_PROVENANCE = {"explicit_eligibility", "agency_inference", "legacy_unknown", "unknown"}
LOCATION_NAMES = sorted({*REGION_ALIASES, *CANONICAL_REGIONS}, key=lambda s: (-len(s), s))
LOCATION_PATTERN = "(?:" + "|".join(map(re.escape, LOCATION_NAMES)) + r")(?:\s+[가-힣0-9]+(?:시|군|구|읍|면|동)){0,3}"


def normalize_region(value):
    """One province/metropolitan name, optionally with districts. Unknown/multiple → None.

    Districts are a retrieval approximation only; A retains the full source evidence.
    전국 is recognized only when explicitly provided, never inferred from missing data.
    """
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    compact = value.replace(" ", "")
    if compact in REGION_ALIASES or compact in CANONICAL_REGIONS:
        return REGION_ALIASES.get(compact, compact)
    # Match C's established short-name + 시/도 user-filter normalization.
    if compact.endswith(("시", "도")) and compact[:-1] in CANONICAL_REGIONS:
        return compact[:-1]
    if value == "전국":
        return value
    if re.fullmatch(LOCATION_PATTERN, value) is None:
        return None
    for name in LOCATION_NAMES:
        if value == name or value.startswith(name + " "):
            return REGION_ALIASES.get(name, name)
    return None


def aliases_for(value):
    canonical = normalize_region(value)
    if canonical is None:
        return []
    return list(dict.fromkeys([canonical, *(name for name, short in REGION_ALIASES.items() if short == canonical),
                               *([canonical + "시", canonical + "도"] if canonical != "전국" else [])]))
