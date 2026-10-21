"""Dataset scope, separate from applicant eligibility (신청 자격). No label lookup."""
import re

POLICY_ID = "personal-household-v1"
# Narrow source assertions about the recipient; a business word alone is insufficient.
INSTITUTION_ONLY = re.compile(r"(?:법인|공공기관|지방자치단체|비영리단체)(?:만을 대상으로|으로만|에 한정|에만|만)(?=$|[\s,.;:·()])")
PERSONAL = ("개인", "가구", "가족", "주민", "근로자", "구직", "학생", "부모", "임산부", "아동", "자녀", "양육", "돌봄")
DOMAINS = {
    "employment": ("고용", "취업", "구직", "근로", "직업", "훈련"),
    "housing_education": ("주거", "주택", "월세", "전세", "교육", "학비", "장학"),
    "income_living": ("소득", "생활", "생계", "장려금", "수당", "의료"),
    "family_care": ("혼인", "결혼", "신혼", "임신", "출산", "양육", "아동", "자녀", "돌봄", "보육"),
}


def inclusion_decision(service):
    from src.ingest.normalize import youth_inclusion_reasons
    youth = [r for r in youth_inclusion_reasons(service) if r["reason"] != "no_reliable_inclusion_evidence"]
    target = service.get("support_target") or ""
    # Contradictory personal applicability prevents a confident corporate-only exclusion.
    only = INSTITUTION_ONLY.search(target)
    negated = any(word in target for word in ("아님", "아니", "않", "제외"))
    if only and not youth and not negated and not any(word in target for word in PERSONAL):
        return {"policy_id": POLICY_ID, "outcome": "exclude", "uncertainty": [],
                "reasons": [{"reason": "explicit_institution_only", "field": "support_target", "evidence": only.group()}]}
    reasons = list(youth)
    for field in ("service_name", "support_target", "selection_criteria", "support_content"):
        text = service.get(field) or ""
        for domain, terms in DOMAINS.items():
            matches = [word for word in terms if word in text]
            if matches:
                reasons.append({"reason": "personal_household_domain", "field": field, "domain": domain, "terms": matches})
    if any(word in target for word in PERSONAL):
        reasons.append({"reason": "personal_household_recipient", "field": "support_target"})
    supported = bool(reasons)
    return {"policy_id": POLICY_ID, "outcome": "include" if supported else "provisional",
            "reasons": reasons or [{"reason": "ambiguous_applicability_high_recall"}],
            "uncertainty": ([] if supported else ["personal_household_applicability_unverified"])
                           + ["individual_eligibility_requires_confirmation"]}


def is_included_service(service):
    return inclusion_decision(service)["outcome"] != "exclude"
