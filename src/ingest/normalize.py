"""결정적 정제. JA* 원시값을 신청자 자격으로 자동 해석하지 않는다."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit

from src.common import config
from src.common.schema import Metadata
from src.common.regions import normalize_region, REGION_PROVENANCE
from src.ingest.regions import apply_region_evidence

NORMALIZATION_VERSION = "region-deadline-v2"

SOURCE_FIELDS = {
    "service_id": "서비스ID", "service_name": "서비스명", "agency": "소관기관명",
    "field": "서비스분야", "support_target": "지원대상", "selection_criteria": "선정기준",
    "support_content": "지원내용", "how_to_apply": "신청방법",
    "application_period": "신청기한", "detail_url": "상세조회URL",
}
METADATA_KEYS = ("region", "agency", "field", "deadline", "age_min", "age_max", "target_tags")


class _TextHTMLParser(HTMLParser):
    """Keep structural separators, including nested table-cell contents."""
    blocks = {"p", "div", "li", "ul", "ol", "tr", "table", "thead", "tbody", "tfoot",
              "section", "article", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
    inline = {"b", "strong", "em", "i", "span", "a", "u", "s", "small", "sup", "sub", "font"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.parts.append("\t")
        elif tag == "br" or tag in self.blocks:
            self.parts.append("\n")
        elif tag not in self.inline:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.parts.append("\t")
        elif tag in self.blocks:
            self.parts.append("\n")
        elif tag != "br" and tag not in self.inline:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)


def clean_text(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise ValueError("invalid text value")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    parser = _TextHTMLParser()
    parser.feed(text)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")).strip()


def parse_deadline(text: str) -> str | None:
    """Strict whole numeric/Korean dates or two whole dates separated by ~ / ～."""
    numeric = r"\d{4}[-./]\d{1,2}[-./]\d{1,2}\.?"
    korean = r"\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일"
    pattern = f"(?:{numeric}|{korean})"
    match = re.fullmatch(r"\s*(" + pattern + r")(?:\s*[~～]\s*(" + pattern + r"))?\s*", text)
    if not match:
        return None
    try:
        start = date(*(int(v) for v in re.findall(r"\d+", match[1])))
        if match[2]:
            end = date(*(int(v) for v in re.findall(r"\d+", match[2])))
            return end.isoformat() if start <= end else None
        return start.isoformat()
    except ValueError:
        return None


def official_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (parts.scheme != "https" or parts.username or parts.password
                or not (host.endswith(".gov.kr") or host.endswith(".go.kr"))
                or any(k.lower() in ("servicekey", "api_key", "token") for k, _ in parse_qsl(parts.query))):
            return ""
        return value
    except ValueError:
        return ""


def verified_eligibility(conditions: dict | None) -> dict:
    """A 소유 어댑터 계약. 신청자/부양자·경계·빈값 의미 검증 전 JA* 값을 변환하지 않는다.

    conditions['_verified_eligibility']는 API 원본 필드가 아니다. 별도 검증된
    어댑터/합성 테스트 전용이며 source='applicant', age_ranges, region을 사용한다.
    """
    data = (conditions or {}).get("_verified_eligibility", {})
    if not isinstance(data, dict) or data.get("source") != "applicant":
        return {}
    result = {}
    ranges = data.get("age_ranges")
    if isinstance(ranges, list) and ranges and all(
        isinstance(pair, list) and len(pair) == 2 and all(type(v) is int for v in pair)
        and 0 <= pair[0] <= pair[1] <= 150 for pair in ranges
    ):
        result["age_ranges"] = sorted(set(tuple(p) for p in ranges))
        result["age_ranges"] = [list(p) for p in result["age_ranges"]]
    region = data.get("region")
    if isinstance(region, str) and clean_text(region):
        result["region"] = clean_text(region)
        result["region_raw"] = region
        result["region_provenance"] = "explicit_eligibility"
        result["region_adapter"] = "internal_verified_applicant_contract_not_API_mapping"
    return result


def content_hash(service: dict) -> str:
    payload = {key: service.get(key) for key in SOURCE_FIELDS}
    payload["metadata"] = build_metadata(service)
    def semantic(value):
        if isinstance(value, dict):
            return {k: semantic(v) for k, v in value.items() if k not in ("observation", "source_acquisition", "acquired_at", "run_id", "legacy_region")}
        return [semantic(v) for v in value] if isinstance(value, list) else value
    payload["eligibility"] = semantic(service.get("eligibility", {}))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def clean_service(raw: dict, conditions: dict | None = None) -> dict:
    if conditions and conditions.get("서비스ID", raw.get("서비스ID")) != raw.get("서비스ID"):
        raise ValueError("condition_id_mismatch")
    service = {key: clean_text(raw.get(source)) for key, source in SOURCE_FIELDS.items()}
    if not service["service_id"] or not service["service_name"]:
        raise ValueError("missing_service_identity")
    service["detail_url"] = official_url(service["detail_url"])
    service["deadline"] = parse_deadline(service["application_period"])
    service["eligibility"] = verified_eligibility(conditions)
    apply_region_evidence(service)
    service["data_quality"] = ["missing_" + key for key in ("agency", "detail_url") if not service[key]]
    from src.ingest.policy import inclusion_decision
    service["inclusion_policy"] = inclusion_decision(service)
    service["inclusion_reasons"] = service["inclusion_policy"]["reasons"]
    service["content_hash"] = content_hash(service)
    return service


def youth_inclusion_reasons(service: dict) -> list[dict]:
    reasons = []
    lo, hi = config.YOUTH_AGE_RANGE
    for age_lo, age_hi in service.get("eligibility", {}).get("age_ranges", []):
        if age_lo <= hi and age_hi >= lo:
            reasons.append({"reason": "verified_age_overlap", "range": [age_lo, age_hi]})
    for field in ("service_name", "support_target", "selection_criteria"):
        for keyword in config.YOUTH_KEYWORDS:
            if keyword in (service.get(field) or ""):
                reasons.append({"reason": "keyword", "field": field, "keyword": keyword})
    return reasons or [{"reason": "no_reliable_inclusion_evidence"}]


def is_youth_service(service: dict) -> bool:
    return any(r["reason"] != "no_reliable_inclusion_evidence" for r in youth_inclusion_reasons(service))


def build_metadata(service: dict) -> Metadata:
    result = {key: service[key] for key in ("agency", "field", "deadline") if service.get(key)}
    eligibility = service.get("eligibility", {})
    canonical_region = normalize_region(eligibility.get("region"))
    if canonical_region:
        result["region"] = canonical_region
        provenance = eligibility.get("region_provenance", "legacy_unknown")
        result["region_provenance"] = provenance if provenance in REGION_PROVENANCE else "legacy_unknown"
    ranges = eligibility.get("age_ranges", [])
    # Multiple/disjoint alternatives cannot be represented by the shared interval.
    if len(ranges) == 1:
        result["age_min"], result["age_max"] = ranges[0]
    tags = sorted({r["keyword"] for r in youth_inclusion_reasons(service) if r["reason"] == "keyword"})
    if tags:
        result["target_tags"] = tags
    return result


def refresh_service(service):
    """Re-derive metadata from CURRENT normalized evidence without re-parsing HTML/text."""
    result = dict(service)
    result["deadline"] = parse_deadline(result.get("application_period") or "")
    apply_region_evidence(result)
    result["content_hash"] = content_hash(result)
    return result
