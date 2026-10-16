import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from benefit_rag.evaluation_contract import EvaluationDataset, check_synthetic_evidence

EXAMPLE = Path("tests/fixtures/evaluation/synthetic.json")


@pytest.fixture
def payload():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def validate(payload):
    return EvaluationDataset.model_validate_json(json.dumps(payload, ensure_ascii=False))


def test_synthetic_contract_and_recoverable_evidence(payload):
    dataset = validate(payload)
    assert dataset.data_kind == "synthetic_contract"
    assert dataset.corpus_snapshot.dataset_kind == "fixture"
    check_synthetic_evidence(dataset, Path.cwd())
    assert dataset.cases[1].relevant_program_ids == []
    assert dataset.cases[0].expected_condition_findings[0].result == "needs_confirmation"


@pytest.mark.parametrize("field", ["notice_ko", "data_kind", "confirmed_profile", "review_notes"])
def test_required_contract_fields(payload, field):
    target = payload if field in {"notice_ko", "data_kind"} else payload["cases"][0]
    target.pop(field)
    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize("mutation", ["namespace", "real_label", "question", "identifier"])
def test_synthetic_cannot_masquerade_as_real_policy(payload, mutation):
    if mutation == "namespace":
        payload["corpus_snapshot"]["dataset_kind"] = "live"
    elif mutation == "real_label":
        payload["data_kind"] = "policy_annotation"
        payload["corpus_snapshot"]["dataset_kind"] = "live"
        payload["corpus_snapshot"]["raw_artifacts"] = {}
    elif mutation == "question":
        payload["cases"][0]["question_ko"] = "실제 정책 질문"
    else:
        payload["cases"][0]["query_id"] = "real-looking-id"
    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "program",
        "version",
        "evidence",
        "span",
        "unknown_rule",
        "closed_no_evidence",
        "unlabeled",
        "reviewed_without_evidence",
    ],
)
def test_cross_reference_and_unknown_rules(payload, mutation):
    case = payload["cases"][0]
    if mutation == "program":
        case["relevant_program_ids"] = ["synthetic:missing-program"]
    elif mutation == "version":
        case["supporting_evidence_references"][0]["program_version_id"] = (
            "synthetic:missing-version"
        )
    elif mutation == "evidence":
        case["expected_response_facts"][0]["evidence_ids"] = ["synthetic:missing-evidence"]
    elif mutation == "span":
        case["supporting_evidence_references"][0]["end_offset"] += 1
    elif mutation == "unknown_rule":
        case["expected_condition_findings"][0]["result"] = "satisfied"
    elif mutation == "closed_no_evidence":
        case["expected_application_availability"][0]["status"] = "closed"
    elif mutation == "unlabeled":
        case["label_completeness"] = "unlabeled"
    else:
        case["annotation_status"] = "reviewed"
        case["supporting_evidence_references"] = []
        case["expected_response_facts"] = []
        case["expected_condition_findings"] = []
    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize("key", ["question_ko", "paraphrase_group_id"])
def test_development_and_held_out_do_not_share_groups(payload, key):
    payload["cases"][1][key] = payload["cases"][0][key]
    with pytest.raises(ValidationError, match="cross_split_query_leakage"):
        validate(payload)


@pytest.mark.parametrize("mutation", ["hash", "excerpt", "path"])
def test_evidence_bytes_and_span_are_checked_offline(payload, mutation):
    evidence = payload["cases"][0]["supporting_evidence_references"][0]
    if mutation == "hash":
        evidence["raw_sha256"] = "0" * 64
    elif mutation == "excerpt":
        evidence["excerpt_ko"] = "X" * len(evidence["excerpt_ko"])
    else:
        payload["corpus_snapshot"]["raw_artifacts"][evidence["raw_response_id"]] = (
            "tests/fixtures/nonexistent.json"
        )
    with pytest.raises(ValueError, match="synthetic_evidence"):
        check_synthetic_evidence(validate(payload), Path.cwd())


def test_profile_rejects_unconfirmed_interpretations_and_sensitive_fields(payload):
    payload["cases"][0]["confirmed_profile"]["precise_address"] = "must not be accepted"
    with pytest.raises(ValidationError):
        validate(payload)


def test_income_ratio_does_not_require_invented_amount(payload):
    payload["cases"][0]["confirmed_profile"]["income"] = {
        "currency": "KRW",
        "scope": "household",
        "period": "unknown",
        "basis": "unknown",
        "measure": "assessed_income",
        "standard_median_income_percentage": "80",
    }
    income = validate(payload).cases[0].confirmed_profile.income
    assert income.amount is None and income.applicable_year is None
    assert income.household_size is None and income.standard_median_income_percentage == "80"
