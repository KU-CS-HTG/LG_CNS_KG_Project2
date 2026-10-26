"""catalog/events/<편집자>.jsonl 에 이벤트를 추가만 한다 (검수 저장소와 같은 방식: 사람마다 파일이 달라 git 충돌이 없다).

이벤트: add(새 제도) / edit(값 바꾸기) / revert(원본 값으로 되돌리기) / hide·unhide(답변에서 숨기기·복구) / remove(직접 추가한 제도 삭제).
API로 받은 제도는 지우지 않고 숨긴다 (다음 수집에서 다시 살아나지 않고, 언제든 복구된다).
덧씌운 결과(`Overlay`)는 A의 DB·색인을 직접 고치지 않는다. 색인에는 `apply`로 따로 반영한다 (src/catalog/apply.py)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "catalog"
SCHEMA = 1
OPS = ("add", "edit", "revert", "hide", "unhide", "remove")
EDITABLE = ("service_name", "agency", "field", "support_target", "selection_criteria", "support_content",
            "how_to_apply", "application_period", "detail_url")
LABELS = {"service_name": "서비스명", "agency": "소관기관", "field": "분야", "support_target": "지원대상",
          "selection_criteria": "선정기준", "support_content": "지원내용", "how_to_apply": "신청방법",
          "application_period": "신청기한", "detail_url": "상세 링크"}
MAX_NAME, MAX_TEXT, MAX_NOTE = 200, 20000, 500
MANUAL_PREFIX = "MAN-"
_EDITOR = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_SECRET_PARAM = re.compile(r"(?i)(servicekey|api_key|token)=")


class CatalogError(ValueError):
    """사용자가 고칠 수 있는 입력 오류 (메시지는 화면에 그대로 보여 준다)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def is_manual(service_id: str) -> bool:
    return service_id.startswith(MANUAL_PREFIX)


def check_fields(fields: dict, *, require_name: bool = False) -> dict:
    """편집 가능한 필드만, 문자열로, 길이 제한 안에서 받는다. 앞뒤 공백은 지운다."""
    if not isinstance(fields, dict):
        raise CatalogError("값이 올바르지 않습니다")
    out = {}
    for key, value in fields.items():
        if key not in EDITABLE:
            raise CatalogError(f"고칠 수 없는 항목입니다: {key}")
        if not isinstance(value, str):
            raise CatalogError(f"{LABELS[key]}은(는) 글이어야 합니다")
        value = value.strip()
        if len(value) > (MAX_NAME if key == "service_name" else MAX_TEXT):
            raise CatalogError(f"{LABELS[key]}이(가) 너무 깁니다")
        if key == "detail_url" and value and not _safe_url(value):
            raise CatalogError("상세 링크는 https:// 로 시작하는 주소여야 합니다 (인증키가 든 주소는 쓸 수 없습니다)")
        out[key] = value
    if "service_name" in out and not out["service_name"]:
        raise CatalogError("서비스명은 비울 수 없습니다")
    if require_name and not out.get("service_name"):
        raise CatalogError("서비스명을 입력하세요")
    return out


def _safe_url(url: str) -> bool:
    return url.startswith("https://") and "@" not in url.split("/")[2] and not _SECRET_PARAM.search(url) and " " not in url


class Overlay:
    """이벤트를 시간 순서로 접은 결과."""

    def __init__(self, events: list[dict]):
        self.events = events
        self.manual: dict[str, dict] = {}        # 직접 추가한 제도: service_id -> 필드
        self.edits: dict[str, dict] = {}         # API 제도의 덮어쓴 값
        self.hidden: set[str] = set()
        self.removed: set[str] = set()
        self.restored: set[str] = set()          # 숨겼다가 푼 제도 (색인에 다시 넣어야 한다)
        self.history: dict[str, list[dict]] = {}
        for e in events:
            sid, op, fields = e["service_id"], e["op"], e.get("fields") or {}
            self.history.setdefault(sid, []).append(e)
            if op == "add":
                self.manual[sid] = dict(fields)
                self.removed.discard(sid)
            elif op in ("edit", "revert") and sid in self.removed:
                continue
            elif op == "edit":
                (self.manual[sid] if sid in self.manual else self.edits.setdefault(sid, {})).update(fields)
            elif op == "revert":
                target = self.edits.get(sid, {})
                for key in (fields.get("fields") or list(target)):
                    target.pop(key, None)
                if not target:
                    self.edits.pop(sid, None)
            elif op == "hide":
                self.hidden.add(sid)
                self.restored.discard(sid)
            elif op == "unhide":
                if sid in self.hidden:
                    self.restored.add(sid)
                self.hidden.discard(sid)
            elif op == "remove" and sid in self.manual:
                self.manual.pop(sid)
                self.removed.add(sid)
                self.hidden.discard(sid)
        self.touched = set(self.manual) | set(self.edits) | self.hidden | self.removed | self.restored

    def is_hidden(self, sid: str) -> bool:
        return sid in self.hidden


class CatalogStore:
    def __init__(self, root: str | Path = ROOT):
        self.root = Path(root)

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    # --- 쓰기 -------------------------------------------------------------------------------------------------
    def _append(self, editor: str, op: str, service_id: str, fields: dict | None = None, note: str = "") -> dict:
        if not _EDITOR.match(editor or ""):
            raise CatalogError("편집자 이름은 영문·숫자·_ . - 만 쓸 수 있습니다 (40자 이내)")
        if len(note) > MAX_NOTE:
            raise CatalogError("메모가 너무 깁니다")
        event = {"event_id": uuid.uuid4().hex, "ts": now_iso(), "editor": editor, "schema": SCHEMA, "op": op,
                 "service_id": service_id, "fields": fields or {}, "note": note.strip()}
        path = self.events_dir / f"{editor}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        needs_newline = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            if needs_newline:
                f.write("\n")
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return event

    def add_service(self, editor: str, fields: dict, note: str = "") -> str:
        fields = check_fields(fields, require_name=True)
        if not (fields.get("support_target") or fields.get("support_content")):
            raise CatalogError("지원대상 또는 지원내용 중 하나는 입력하세요")
        sid = MANUAL_PREFIX + hashlib.sha1(f"{fields['service_name']}{now_iso()}{uuid.uuid4()}".encode()).hexdigest()[:8]
        self._append(editor, "add", sid, fields, note)
        return sid

    def edit_service(self, editor: str, service_id: str, fields: dict, note: str = "") -> dict | None:
        """바뀐 값만 기록한다. 바뀐 것이 없으면 None."""
        fields = check_fields(fields)
        if not fields:
            return None
        return self._append(editor, "edit", service_id, fields, note)

    def revert(self, editor: str, service_id: str, keys: list[str] | None = None) -> dict:
        bad = [k for k in keys or [] if k not in EDITABLE]
        if bad:
            raise CatalogError(f"고칠 수 없는 항목입니다: {bad}")
        return self._append(editor, "revert", service_id, {"fields": list(keys or [])})

    def hide(self, editor: str, service_id: str, note: str = "") -> dict:
        return self._append(editor, "hide", service_id, note=note)

    def unhide(self, editor: str, service_id: str, note: str = "") -> dict:
        return self._append(editor, "unhide", service_id, note=note)

    def remove_manual(self, editor: str, service_id: str, note: str = "") -> dict:
        if not is_manual(service_id):
            raise CatalogError("직접 추가한 제도만 삭제할 수 있습니다. API 제도는 숨기기를 쓰세요")
        return self._append(editor, "remove", service_id, note=note)

    # --- 읽기 -------------------------------------------------------------------------------------------------
    def read_events(self) -> list[dict]:
        events, seen = [], set()
        for n, path in enumerate(sorted(self.events_dir.glob("*.jsonl")) if self.events_dir.exists() else []):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    if e["op"] not in OPS or not isinstance(e["service_id"], str) or e["event_id"] in seen:
                        continue
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue                       # 깨진 줄은 건너뛴다 (나머지 이벤트는 그대로 쓴다)
                seen.add(e["event_id"])
                e["_n"] = len(events)
                events.append(e)
        events.sort(key=lambda e: (e["ts"], e["_n"]))
        return events

    def overlay(self) -> Overlay:
        return Overlay(self.read_events())


_cache: dict[tuple, tuple[tuple, Overlay]] = {}


def hidden_ids(root: str | Path = ROOT) -> frozenset[str]:
    """답변에서 숨길 제도 ID. 파일이 바뀐 때만 다시 읽는다 (질문마다 부르므로)."""
    root = Path(root)
    events = root / "events"
    stamp = tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in events.glob("*.jsonl"))) if events.exists() else ()
    key = (str(root),)
    if key not in _cache or _cache[key][0] != stamp:
        _cache[key] = (stamp, CatalogStore(root).overlay())
    return frozenset(_cache[key][1].hidden)
