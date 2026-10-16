import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from benefit_rag.domain import NORMALIZATION_VERSION, Evidence, Page, ProgramData

ENDPOINT = "https://api.odcloud.kr/api/gov24/v3/serviceList"
VOLATILE_FIELDS = {"조회수", "수정일시", "등록일시"}
TEXT_FIELDS = {
    "name": "서비스명",
    "administering_organization": "소관기관명",
    "category": "서비스분야",
    "audience_type": "사용자구분",
    "target_text": "지원대상",
    "benefit_text": "지원내용",
    "purpose_text": "서비스목적요약",
    "eligibility_text": "선정기준",
    "application_period_text": "신청기한",
    "application_method_text": "신청방법",
    "source_updated_text": "수정일시",
}
DOCUMENT_FIELDS = {
    "name",
    "target_text",
    "benefit_text",
    "purpose_text",
    "eligibility_text",
    "application_period_text",
    "application_method_text",
}


class SourceError(Exception):
    """인증키·요청 URL·응답 본문을 포함하지 않는 오류 코드만 전달."""


@dataclass(frozen=True)
class Response:
    body: bytes
    status: int = 200


class SourceAdapter(Protocol):
    def fetch_page(self, page: int, page_size: int) -> Response: ...


class FixtureAdapter:
    def __init__(self, directory: Path):
        self.directory = directory
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("dataset_kind") != "synthetic" or manifest.get("source") != "gov24":
            raise SourceError("fixture_manifest_invalid")

    def fetch_page(self, page: int, page_size: int) -> Response:
        try:
            return Response((self.directory / f"page-{page:04d}.json").read_bytes())
        except OSError:
            raise SourceError("fixture_page_missing") from None


class Gov24Adapter:
    def __init__(self, client: httpx.Client, service_key: str, *, sleep=time.sleep):
        if not service_key.strip():
            raise SourceError("credential_missing")
        self.client = client
        self._service_key = service_key
        self._sleep = sleep

    def fetch_page(self, page: int, page_size: int) -> Response:
        for attempt in range(3):
            try:
                response = self.client.get(
                    ENDPOINT,
                    params={
                        "page": page,
                        "perPage": page_size,
                        "returnType": "JSON",
                        "serviceKey": self._service_key,
                    },
                    timeout=httpx.Timeout(20, connect=5, pool=5),
                    follow_redirects=False,
                )
            except httpx.TransportError:
                if attempt == 2:
                    raise SourceError("transport_retries_exhausted") from None
                self._sleep(2**attempt)
                continue
            result = Response(response.content, response.status_code)
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                return result
            delay = float(2**attempt)
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        date = parsedate_to_datetime(retry_after)
                        delay = max(delay, (date - datetime.now(UTC)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            # 서버가 요구한 대기보다 일찍 재요청하지 않고 이번 수집을 실패 처리한다.
            if delay > 30:
                return result
            self._sleep(delay)
        raise AssertionError("unreachable")


def parse_page(body: bytes, page_number: int, page_size: int) -> Page:
    page = Page.model_validate_json(body)
    if page.page != page_number or page.perPage != page_size:
        raise SourceError("pagination_mismatch")
    if (
        page.currentCount != len(page.data)
        or page.currentCount > page_size
        or page.matchCount > page.totalCount
        or page.currentCount > page.matchCount
    ):
        raise SourceError("count_mismatch")
    return page


def content_hash(record: dict) -> str:
    content = {k: v for k, v in record.items() if k not in VOLATILE_FIELDS}
    encoded = json.dumps(
        {"normalizer": NORMALIZATION_VERSION, "content": content},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize(record: object, index: int) -> ProgramData:
    if not isinstance(record, dict):
        raise SourceError("record_not_object")
    identifier = record.get("서비스ID")
    name = record.get("서비스명")
    if (
        not isinstance(identifier, str)
        or not identifier.strip()
        or len(identifier) > 128
        or not isinstance(name, str)
        or not name.strip()
    ):
        raise SourceError("required_field_invalid")
    data = ProgramData(source_record_id=identifier, name=name)
    for field, source_field in TEXT_FIELDS.items():
        value = record.get(source_field)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            data.quality_issues.append(f"invalid_type:{field}")
            continue
        if not value.strip():
            continue
        setattr(data, field, value)
        data.evidence[field] = Evidence(
            source_field=source_field, json_pointer=f"/data/{index}/{source_field}"
        )
    url = record.get("상세조회URL")
    if url:
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (
                parsed
                and parsed.scheme in {"https", "http"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            ):
                data.official_url = url
                data.url_status = "unverified"
                data.evidence["official_url"] = Evidence(
                    source_field="상세조회URL", json_pointer=f"/data/{index}/상세조회URL"
                )
            else:
                data.quality_issues.append("invalid_url")
        except ValueError:
            data.quality_issues.append("invalid_url")
    # 소관기관명은 관할 지역의 증거가 아니다. 임의 지역·날짜·자격 규칙 추출을 하지 않는다.
    return data
