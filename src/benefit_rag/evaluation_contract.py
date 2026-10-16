"""평가 주석의 구조·참조 일관성 검증. 정책의 사실성이나 라벨의 정답을 인증하지 않는다."""

import hashlib
import json
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from benefit_rag.eligibility import ConditionResult


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncomeObservation(Contract):
    amount: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?$")
    currency: Literal["KRW"]
    scope: Literal["individual", "household", "unknown"]
    period: Literal["monthly", "annual", "unknown"]
    basis: Literal["gross", "net", "unknown"]
    measure: Literal["income", "assessed_income", "unknown"]
    applicable_year: int | None = Field(default=None, ge=1900, le=2200, strict=True)
    household_size: int | None = Field(default=None, ge=1, strict=True)
    standard_median_income_percentage: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?$")


class ConfirmedProfile(Contract):
    age: int | None = Field(default=None, ge=0, le=130, strict=True)
    residence_region: str | None = None
    employment_status: str | None = None
    household_size: int | None = Field(default=None, ge=1, strict=True)
    household_composition: list[str] = Field(default_factory=list)
    income: IncomeObservation | None = None
    interests: list[str] = Field(default_factory=list)


class ProgramReference(Contract):
    program_id: str = Field(min_length=1)
    program_version_id: str = Field(min_length=1)


class EvidenceReference(ProgramReference):
    evidence_id: str = Field(min_length=1)
    raw_response_id: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: str | None = None
    json_pointer: str = Field(pattern=r"^/data/\d+/[^/]+$")
    start_offset: int = Field(ge=0, strict=True)
    end_offset: int = Field(ge=1, strict=True)
    excerpt_ko: str = Field(min_length=1)

    @model_validator(mode="after")
    def span_matches_excerpt(self):
        if self.end_offset - self.start_offset != len(self.excerpt_ko):
            raise ValueError("evidence_span_length_mismatch")
        return self


class ConditionFinding(ProgramReference):
    condition_id: str = Field(min_length=1)
    condition_text_ko: str = Field(min_length=1)
    result: ConditionResult
    validation_status: Literal["validated", "unvalidated", "ambiguous", "missing"]
    evidence_ids: list[str]

    @model_validator(mode="after")
    def unknown_stays_unknown(self):
        if (
            self.validation_status != "validated"
            and self.result != ConditionResult.NEEDS_CONFIRMATION
        ):
            raise ValueError("unvalidated_condition_must_need_confirmation")
        if self.validation_status == "validated" and not self.evidence_ids:
            raise ValueError("condition_evidence_required")
        return self


class ApplicationFinding(ProgramReference):
    application_round_id: str | None
    status: Literal["open", "upcoming", "closed", "ongoing", "unknown"]
    evidence_ids: list[str]

    @model_validator(mode="after")
    def known_availability_requires_evidence(self):
        if self.status != "unknown" and not self.evidence_ids:
            raise ValueError("application_evidence_required")
        return self


class ResponseFact(ProgramReference):
    fact_id: str = Field(min_length=1)
    text_ko: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class EvaluationCase(Contract):
    query_id: str = Field(min_length=1)
    question_ko: str = Field(min_length=1)
    split: Literal["development", "held_out"]
    paraphrase_group_id: str = Field(min_length=1)
    confirmed_profile: ConfirmedProfile
    evaluation_date: date = Field(strict=True)
    corpus_snapshot_id: str = Field(min_length=1)
    scenario_tags: list[str] = Field(min_length=1)
    relevant_program_ids: list[str]
    supporting_evidence_references: list[EvidenceReference]
    expected_condition_findings: list[ConditionFinding]
    expected_application_availability: list[ApplicationFinding]
    expected_response_facts: list[ResponseFact]
    forbidden_or_unsupported_claims: list[str]
    annotation_status: Literal["draft", "reviewed"]
    label_completeness: Literal["complete", "partial", "unlabeled"]
    review_notes: str = Field(min_length=1)


class CorpusSnapshot(Contract):
    snapshot_id: str = Field(min_length=1)
    source: Literal["gov24"]
    dataset_kind: Literal["fixture", "live"]
    program_versions: list[ProgramReference]
    raw_artifacts: dict[str, str] = Field(default_factory=dict)


class EvaluationDataset(Contract):
    schema_version: Literal["evaluation-v1"]
    data_kind: Literal["synthetic_contract", "policy_annotation"]
    notice_ko: str = Field(min_length=1)
    corpus_snapshot: CorpusSnapshot
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def check_references_and_label_boundaries(self):
        snapshot = self.corpus_snapshot
        pairs = {(p.program_id, p.program_version_id) for p in snapshot.program_versions}
        if len({p.program_id for p in snapshot.program_versions}) != len(snapshot.program_versions):
            raise ValueError("snapshot_requires_one_version_per_program")
        synthetic = self.data_kind == "synthetic_contract"
        if snapshot.dataset_kind != ("fixture" if synthetic else "live"):
            raise ValueError("evaluation_namespace_mismatch")
        if not synthetic and snapshot.raw_artifacts:
            raise ValueError("policy_evidence_requires_database_raw_references")
        for raw_id, path in snapshot.raw_artifacts.items():
            parts = PurePosixPath(path).parts
            if (
                not raw_id.startswith("synthetic:")
                or parts[:2] != ("tests", "fixtures")
                or ".." in parts
            ):
                raise ValueError("synthetic_artifact_path_invalid")
        if synthetic and "합성" not in self.notice_ko:
            raise ValueError("synthetic_notice_required")
        identifiers = [snapshot.snapshot_id, *(v for pair in pairs for v in pair)]
        query_ids, groups, questions = set(), {}, {}
        for case in self.cases:
            if case.query_id in query_ids:
                raise ValueError("duplicate_query_id")
            query_ids.add(case.query_id)
            for key, mapping in ((case.paraphrase_group_id, groups), (case.question_ko, questions)):
                if key in mapping and mapping[key] != case.split:
                    raise ValueError("cross_split_query_leakage")
                mapping[key] = case.split
            if case.corpus_snapshot_id != snapshot.snapshot_id:
                raise ValueError("corpus_snapshot_mismatch")
            if len(set(case.relevant_program_ids)) != len(case.relevant_program_ids):
                raise ValueError("duplicate_relevance_label")
            if not set(case.relevant_program_ids) <= {p for p, _ in pairs}:
                raise ValueError("unknown_relevant_program")
            evidence = {e.evidence_id: e for e in case.supporting_evidence_references}
            if len(evidence) != len(case.supporting_evidence_references):
                raise ValueError("duplicate_evidence_id")
            for ref in evidence.values():
                if (ref.program_id, ref.program_version_id) not in pairs:
                    raise ValueError("evidence_version_not_in_snapshot")
                identifiers.extend([ref.evidence_id, ref.raw_response_id])
                if ref.document_id:
                    identifiers.append(ref.document_id)
                if synthetic and ref.raw_response_id not in snapshot.raw_artifacts:
                    raise ValueError("synthetic_evidence_artifact_missing")
            for finding in [
                *case.expected_condition_findings,
                *case.expected_application_availability,
                *case.expected_response_facts,
            ]:
                pair = finding.program_id, finding.program_version_id
                if pair not in pairs:
                    raise ValueError("finding_version_not_in_snapshot")
                for evidence_id in finding.evidence_ids:
                    if (
                        evidence_id not in evidence
                        or (
                            evidence[evidence_id].program_id,
                            evidence[evidence_id].program_version_id,
                        )
                        != pair
                    ):
                        raise ValueError("finding_evidence_mismatch")
            for labels, key in (
                (case.expected_condition_findings, "condition_id"),
                (case.expected_response_facts, "fact_id"),
            ):
                values = [getattr(label, key) for label in labels]
                if len(set(values)) != len(values):
                    raise ValueError("duplicate_finding_id")
                identifiers.extend(values)
            identifiers.extend(
                f.application_round_id
                for f in case.expected_application_availability
                if f.application_round_id is not None
            )
            if case.label_completeness == "unlabeled" and (
                case.relevant_program_ids
                or case.expected_condition_findings
                or case.expected_application_availability
                or case.expected_response_facts
            ):
                raise ValueError("unlabeled_case_has_labels")
            if case.annotation_status == "reviewed" and case.label_completeness != "unlabeled":
                if not set(case.relevant_program_ids) <= {e.program_id for e in evidence.values()}:
                    raise ValueError("reviewed_relevance_requires_evidence")
            identifiers.extend(
                [case.query_id, case.paraphrase_group_id, *case.relevant_program_ids]
            )
            if synthetic and not case.question_ko.startswith("[합성]"):
                raise ValueError("synthetic_question_marker_required")
        if synthetic and any(not value.startswith("synthetic:") for value in identifiers):
            raise ValueError("synthetic_identifiers_required")
        if not synthetic and any(value.startswith("synthetic:") for value in identifiers):
            raise ValueError("synthetic_data_cannot_be_policy_annotation")
        return self


def check_synthetic_evidence(dataset: EvaluationDataset, root: Path) -> None:
    """합성 예제의 bytes/SHA/JSON pointer/span만 확인. relevance·정책 의미는 판정하지 않는다."""
    if dataset.data_kind != "synthetic_contract":
        raise ValueError("synthetic_dataset_required")
    fixture_root = (root / "tests" / "fixtures").resolve()
    for case in dataset.cases:
        for ref in case.supporting_evidence_references:
            path = (root / dataset.corpus_snapshot.raw_artifacts[ref.raw_response_id]).resolve()
            if not path.is_relative_to(fixture_root):
                raise ValueError("synthetic_artifact_outside_fixture_root")
            try:
                body = path.read_bytes()
                payload = json.loads(body)
                _, _, index, field = ref.json_pointer.split("/")
                value = payload["data"][int(index)][field.replace("~1", "/").replace("~0", "~")]
                valid = (
                    isinstance(value, str)
                    and ref.end_offset <= len(value)
                    and value[ref.start_offset : ref.end_offset] == ref.excerpt_ko
                    and hashlib.sha256(body).hexdigest() == ref.raw_sha256
                )
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                raise ValueError("synthetic_evidence_unrecoverable") from None
            if not valid:
                raise ValueError("synthetic_evidence_mismatch")
