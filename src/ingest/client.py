"""보조금24 수집. v3 세부 계약은 미검증이며 기본 수집은 소실 판정을 허가하지 않는다."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests
from src.common import config

ENDPOINTS = ("serviceList", "serviceDetail", "supportConditions")
# First-page IDs/shape observed in pass 2; full traversal/empty-snapshot authority
# remains unverified. A small smoke sample must never enable disappearance.
VERIFIED_CONTRACTS = {name: False for name in ENDPOINTS}


class CollectionError(RuntimeError):
    """Controlled diagnostic codes only, never request/exception representations."""


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if any(s in k.lower() for s in
                ("servicekey", "authorization", "api_key")) else redact(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        key = config.GOV24_API_KEY
        if key:
            for secret in {key, quote(key, safe=""), quote_plus(key)}:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r'(?i)(serviceKey|api_key)=([^\s&"<>]+)', r'\1=[REDACTED]', value)
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]


def _retry_delay(response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        delay = float(header)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = 2 ** attempt
    return max(0, min(delay, 30))


def _get(endpoint: str, **params) -> dict:
    if endpoint not in ENDPOINTS:
        raise ValueError("unknown endpoint")
    if not config.GOV24_API_KEY:
        raise CollectionError("missing_credentials")
    for attempt in range(3):
        response = None
        code, retry = "transport_error", True
        try:
            response = requests.get(
                f"{config.GOV24_API_BASE}/{endpoint}",
                params={**params, "serviceKey": config.GOV24_API_KEY}, timeout=(10, 30),
            )
            status = response.status_code
            if status != 200:
                code = f"http_{status}"
                retry = status in (408, 429, 500, 502, 503, 504)
            else:
                try:
                    payload = response.json()
                except ValueError:
                    raise CollectionError("malformed_json") from None
                if not isinstance(payload, dict):
                    raise CollectionError("malformed_envelope")
                api_code = str(payload.get("code", payload.get("resultCode", "")))
                if api_code not in ("", "0", "00", "200", "INFO-000"):
                    code = "api_error"
                    retry = api_code in ("01", "05", "23", "429", "500", "503")
                else:
                    return payload
        except (requests.Timeout, requests.ConnectionError):
            pass
        except requests.RequestException:
            raise CollectionError("request_error") from None
        if not retry or attempt == 2:
            raise CollectionError(code) from None
        time.sleep(_retry_delay(response, attempt))
    raise CollectionError("retry_exhausted")


@dataclass
class Collection:
    endpoint: str
    rows: list[dict] = field(default_factory=list)
    status: str = "failed"
    pages: int = 0
    total: int | None = None
    verified: bool = False
    authoritative: bool = False
    limited: bool = False
    diagnostics: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    failed_page: int | None = None

    @property
    def can_close_missing(self) -> bool:
        return (self.endpoint == "serviceList" and self.status == "complete"
                and self.verified and self.authoritative and not self.limited
                and not self.diagnostics and not self.conflicts
                and self.total is not None and self.total > 0
                and self.total == len(self.rows)
                and len({r.get("서비스ID") for r in self.rows}) == self.total)

    def summary(self) -> dict:
        result = asdict(self)
        result["count"] = len(result.pop("rows"))
        result["can_close_missing"] = self.can_close_missing
        return result


def collect_endpoint(endpoint: str, *, run_id: str, per_page: int = 500,
                     max_pages: int | None = None, fetch_page=None,
                     raw_dir: Path | None = None) -> Collection:
    """Candidate v3 pagination; an explicit max_pages always means sample."""
    if endpoint not in ENDPOINTS:
        raise ValueError("unknown endpoint")
    if type(per_page) is not int or not 1 <= per_page <= 1000:
        raise ValueError("per_page must be 1..1000")
    if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
        raise ValueError("max_pages must be positive")
    if Path(run_id).name != run_id or run_id in ("", ".", ".."):
        raise ValueError("invalid run_id")
    result = Collection(endpoint, verified=VERIFIED_CONTRACTS[endpoint],
                        authoritative=endpoint == "serviceList", limited=max_pages is not None)
    root = (Path(raw_dir) if raw_dir is not None else config.RAW_DIR) / run_id
    accepted: dict[str, dict] = {}
    fingerprints: set[str] = set()
    received, page = 0, 1
    try:
        while True:
            payload = (fetch_page or _get)(endpoint, page=page, perPage=per_page, returnType="JSON")
            write_json(root / f"{endpoint}-{page:05}.json", payload)
            result.pages += 1
            rows = redact(payload.get("data"))
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise CollectionError("missing_or_invalid_data")
            for key in ("page", "perPage", "currentCount", "totalCount"):
                if type(payload.get(key)) is not int or payload[key] < 0:
                    raise CollectionError("invalid_pagination_fields")
            if (payload["page"] != page or payload["perPage"] != per_page
                    or payload["currentCount"] != len(rows) or len(rows) > per_page):
                raise CollectionError("pagination_mismatch")
            total = payload["totalCount"]
            if "matchCount" in payload and (type(payload["matchCount"]) is not int
                                            or payload["matchCount"] != total):
                raise CollectionError("unexpected_match_count")
            if result.total is not None and total != result.total:
                raise CollectionError("inconsistent_totals")
            result.total = total
            if total == 0:
                raise CollectionError("unverified_empty_snapshot")
            digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if digest in fingerprints:
                raise CollectionError("repeated_page")
            fingerprints.add(digest)
            if not rows or received + len(rows) > total:
                raise CollectionError("premature_empty_or_excess_rows")
            for row in rows:
                sid = row.get("서비스ID")
                if not isinstance(sid, str) or not sid.strip() or sid != sid.strip():
                    raise CollectionError("invalid_service_id")
                if sid in result.conflicts:
                    continue
                if sid in accepted:
                    result.diagnostics.append("duplicate_id")
                    if accepted[sid] != row:
                        result.conflicts.append(sid)
                        del accepted[sid]
                else:
                    accepted[sid] = row
            received += len(rows)
            if received == total:
                result.status = "complete"
                break
            if len(rows) != per_page:
                raise CollectionError("premature_short_page")
            if max_pages is not None and page >= max_pages:
                result.status = "sample"
                break
            page += 1
            if page > 100000:
                raise CollectionError("page_safety_limit")
    except CollectionError as exc:
        result.failed_page = page
        result.diagnostics.append(str(exc))
        result.status = "partial" if accepted else "failed"
    result.rows = list(accepted.values())
    if result.diagnostics and result.status == "complete":
        result.status = "partial"
    if max_pages is not None and result.status in ("complete", "sample"):
        result.status = "sample"
    write_json(root / f"{endpoint}-summary.json", result.summary())
    return result


def fetch_all(endpoint: str, per_page: int = 500) -> Iterator[dict]:
    result = collect_endpoint(endpoint, run_id=new_run_id(), per_page=per_page)
    if result.status != "complete":
        raise CollectionError(f"{endpoint}_{result.status}")
    yield from result.rows


def save_raw(endpoint: str, rows: list[dict]) -> None:
    if endpoint not in ENDPOINTS:
        raise ValueError("unknown endpoint")
    write_json(config.RAW_DIR / new_run_id() / f"{endpoint}.json", rows)
