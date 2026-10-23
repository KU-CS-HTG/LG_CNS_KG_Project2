"""Conservative residence extraction from complete, scoped source statements only."""
import re

from src.common.regions import LOCATION_PATTERN, LOCATION_NAMES, normalize_region

REGION_ADAPTER_VERSION = "residence-lines-v1"
_RESIDENCE = re.compile(
    r"(?:신청자(?:는|가)?\s*)?(?P<place>전국|" + LOCATION_PATTERN + r")\s*"
    r"(?:에\s*(?:주민등록을\s*두고\s*)?거주(?:하는|하고\s*있는)|\s*거주(?:자|하는|하고\s*있는))"
    r"(?P<recipient>.*)")
_UNSAFE = ("또는", "및", "아니", "아님", "않", "제외", "이외", "불문", "무관", "과거", "이전", "종전",
           "자녀", "아동", "부양", "학교", "소재", "주소", "기관", "접수", "방문", "전입 예정",
           "출신", "혹은", "이거나", "직원", "임직원", "종사자", "근무", "둔", "배우자", "부친", "모친",
           "타지역", "타 지역", "다른 지역", "비거주", "제한 없", "제한없", "예외", "다만", "단,",
           "고용한", "사업주", "사업장", "대표", "설립", "소유")


def residence_evidence(service):
    evidence, unresolved = [], []
    for field in ("support_target", "selection_criteria"):
        for original in (service.get(field) or "").splitlines():
            line = re.sub(r"^[\s○●·ㆍ*\-]+", "", original).strip()
            match = _RESIDENCE.fullmatch(line)
            if not match:
                continue
            place, recipient = match["place"], match["recipient"]
            record = {"field": field, "source_phrase": original, "raw_region": place,
                      "source_field": {"support_target": "지원대상", "selection_criteria": "선정기준"}[field],
                      "canonical_region": normalize_region(place), "rule": REGION_ADAPTER_VERSION,
                      "scope": "district_limited_province_approximation" if " " in place else "province_or_nationwide"}
            provenance = service.get("_field_provenance", {}).get(field)
            if provenance:
                record.update(endpoint=provenance["source"], observation=provenance["observation"],
                              source_acquisition=provenance.get("source_acquisition"))
            else:
                # clean_service alone does not know whether a merged value came from listing/detail.
                record["endpoint"] = "unresolved_until_join"
            if place == "광주시" or any(word in line for word in _UNSAFE) or "/" in line or any(name in recipient for name in LOCATION_NAMES):
                unresolved.append({**record, "reason": "ambiguous_compound_or_non_applicant_residence"})
            else:
                evidence.append(record)
    return evidence, unresolved


def apply_region_evidence(service):
    """Retain full locations/alternatives; expose only a single supported province approximation."""
    eligibility = dict(service.get("eligibility", {}))
    # Recompute this adapter's derived values when the source statement changes/removes.
    if str(eligibility.get("region_rule", "")).startswith("residence-lines-"):
        for key in ("region", "region_raw", "region_provenance", "region_rule"):
            eligibility.pop(key, None)
    evidence, unresolved = residence_evidence(service)
    eligibility["residence_evidence"] = evidence
    eligibility["residence_unresolved"] = unresolved
    regions = {e["canonical_region"] for e in evidence}
    supported_single = bool(evidence) and not unresolved and len(regions) == 1 and None not in regions
    if eligibility.get("region") and eligibility.get("region_provenance") != "explicit_eligibility" and supported_single:
        eligibility["legacy_region"] = {"value": eligibility["region"],
                                       "provenance": eligibility.get("region_provenance", "legacy_unknown")}
        for key in ("region", "region_raw", "region_provenance"):
            eligibility.pop(key, None)
    if eligibility.get("region"):
        eligibility.setdefault("region_raw", eligibility["region"])
        canonical = normalize_region(eligibility["region"])
        if canonical:
            eligibility["region"] = canonical
        # Existing untagged values remain legacy; synthetic adapter tags itself separately.
    elif supported_single:
        locations = {e["raw_region"] for e in evidence}
        eligibility.update(region=next(iter(regions)), region_raw=sorted(locations),
                           region_provenance="explicit_eligibility", region_rule=REGION_ADAPTER_VERSION)
    service["eligibility"] = eligibility
    return service
