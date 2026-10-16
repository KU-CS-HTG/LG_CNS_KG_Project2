from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NORMALIZATION_VERSION = "gov24-list-v1-provisional"
DatasetKind = Literal["fixture", "live"]


class Evidence(BaseModel):
    source_field: str
    json_pointer: str


class ProgramData(BaseModel):
    """공식 명세와 합성 fixture에 기반한 잠정 스키마. 한국어 원문은 raw에도 보존."""

    model_config = ConfigDict(extra="forbid")

    source_record_id: str
    name: str
    administering_organization: str | None = None
    category: str | None = None
    geographic_coverage: str | None = None
    audience_type: str | None = None
    target_text: str | None = None
    benefit_text: str | None = None
    purpose_text: str | None = None
    eligibility_text: str | None = None
    eligibility_status: Literal["unresolved"] = "unresolved"
    application_period_text: str | None = None
    application_method_text: str | None = None
    application_availability: Literal["unknown"] = "unknown"
    official_url: str | None = None
    url_status: Literal["missing", "unverified"] = "missing"
    source_updated_text: str | None = None
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    quality_issues: list[str] = Field(default_factory=list)


class Page(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    page: int = Field(ge=1)
    perPage: int = Field(ge=1)
    totalCount: int = Field(ge=0)
    currentCount: int = Field(ge=0)
    matchCount: int = Field(ge=0)
    data: list[Any]
