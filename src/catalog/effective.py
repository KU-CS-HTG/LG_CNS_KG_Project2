"""API 제도(읽기 전용 DB) + 덧씌운 변경 = 실제로 답변에 쓸 제도 목록."""
from __future__ import annotations

from datetime import date

from src.catalog.store import EDITABLE, Overlay, is_manual


def _manual_service(sid: str, fields: dict) -> dict:
    """직접 추가한 제도를 A의 정규화 규칙(기한 해석·지역 근거)으로 만든다. 상세 링크만은 gov.kr 제한을 두지 않는다."""
    from src.ingest.normalize import clean_service

    raw = {"서비스ID": sid, "서비스명": fields.get("service_name", ""), "소관기관명": fields.get("agency", ""),
           "서비스분야": fields.get("field", ""), "지원대상": fields.get("support_target", ""),
           "선정기준": fields.get("selection_criteria", ""), "지원내용": fields.get("support_content", ""),
           "신청방법": fields.get("how_to_apply", ""), "신청기한": fields.get("application_period", "")}
    service = clean_service(raw)
    service["detail_url"] = fields.get("detail_url", "")
    return service


def effective_service(sid: str, api: dict | None, overlay: Overlay) -> dict | None:
    """sid의 최종 레코드 (없거나 삭제됐으면 None). 숨김 여부는 레코드에 넣지 않는다 (overlay.hidden으로 본다)."""
    if sid in overlay.manual:
        return _manual_service(sid, overlay.manual[sid])
    if api is None:
        return None
    edits = overlay.edits.get(sid)
    if not edits:
        return api
    from src.ingest.normalize import refresh_service

    return refresh_service({**api, **{k: v for k, v in edits.items() if k in EDITABLE}})


def listing(api_services: dict[str, dict], overlay: Overlay, today: date | str | None = None) -> list[dict]:
    """화면용 한 줄 요약 목록. 값은 덧씌운 뒤의 값이다."""
    rows = []
    for sid in [*api_services, *[s for s in overlay.manual if s not in api_services]]:
        record = effective_service(sid, api_services.get(sid), overlay)
        if record is None:
            continue
        rows.append({"service_id": sid, "service_name": record.get("service_name", ""), "agency": record.get("agency", ""),
                     "field": record.get("field", ""), "application_period": record.get("application_period", ""),
                     "source": "직접 추가" if is_manual(sid) else "API", "hidden": overlay.is_hidden(sid),
                     "edited": sid in overlay.edits})
    return rows
