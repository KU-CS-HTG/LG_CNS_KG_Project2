"""reviews/events/<검수자>.jsonl 에 이벤트를 추가만 한다. 사람마다 파일이 따로라서 git에서 충돌하지 않는다."""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.review import schema
from src.review.state import State

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "reviews"


class ReviewStore:
    def __init__(self, root: str | Path = DEFAULT_ROOT):
        self.root = Path(root)
        self.last_written = False        # 직전 add_*가 실제로 파일에 썼는지 (같은 client_event_id면 False)

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    def adjudicators(self) -> tuple[str, ...]:
        path = self.root / "reviewers.json"
        if not path.is_file():
            return ()
        return tuple(json.loads(path.read_text(encoding="utf-8")).get("adjudicators", []))

    # --- 쓰기 -------------------------------------------------------------------------------------------------
    def _file(self, reviewer: str) -> Path:
        return self.events_dir / f"{reviewer}.jsonl"

    def append(self, event: dict) -> bool:
        """검증한 뒤 그 검수자의 파일 끝에 한 줄 추가. 같은 client_event_id가 이미 있으면 쓰지 않고 False (재실행·재시도 방지)."""
        schema.validate_event(event)
        path = self._file(event["reviewer"])
        path.parent.mkdir(parents=True, exist_ok=True)
        client_id = event.get("client_event_id")
        if client_id and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("client_event_id") == client_id:
                        return False
                except json.JSONDecodeError:
                    continue
        needs_newline = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")   # 이전 쓰기가 중간에 끊긴 경우
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            if needs_newline:
                f.write("\n")
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True

    def _emit(self, type_: str, reviewer: str, data: dict, **kw) -> dict:
        event = schema.make_event(type_, data, reviewer, **kw)
        self.last_written = self.append(event)
        return event

    def add_question(self, reviewer: str, text: str, *, source: str = "team", **fields) -> str:
        """질문 등록. 같은 문장이면 같은 question_id가 된다. split을 주지 않으면 8:2로 정해진다."""
        qid = schema.question_id(text)
        client_event_id = fields.pop("client_event_id", None)
        self._emit("question", reviewer, {"question_id": qid, "text": text, "source": source, **fields}, client_event_id=client_event_id)
        return qid

    def set_status(self, reviewer: str, qid: str, status: str, **fields) -> dict:
        client_event_id = fields.pop("client_event_id", None)
        return self._emit("question_status", reviewer, {"question_id": qid, "status": status, **fields}, client_event_id=client_event_id)

    def add_run(self, reviewer: str, qid: str, candidates: list[dict], *, system: dict, answer: dict | None = None,
                shuffled: bool = True, run_id: str | None = None, client_event_id: str | None = None) -> str:
        run_id = run_id or "r_" + schema.new_event_id()[:12]
        data = {"run_id": run_id, "question_id": qid, "system": system, "candidates": candidates, "shuffled": shuffled,
                "k_shown": len(candidates)}
        if answer is not None:
            data["answer"] = answer
        self._emit("run", reviewer, data, client_event_id=client_event_id)
        return run_id

    def add_judgment(self, reviewer: str, qid: str, service_id: str, service_version: str, label: str, *, reason: str | None = None,
                     note: str | None = None, by: str = "human", run_id: str | None = None, client_event_id: str | None = None) -> dict:
        data = {"question_id": qid, "service_id": service_id, "service_version": service_version, "label": label, "reason": reason,
                "note": note, "provenance": {"by": by}}
        if run_id:
            data["run_id"] = run_id
        return self._emit("judgment", reviewer, data, client_event_id=client_event_id)

    def add_memo(self, reviewer: str, run_id: str, kind: str, text: str, *, scope: dict | None = None, severity: str = "minor",
                 client_event_id: str | None = None) -> dict:
        return self._emit("answer_memo", reviewer, {"run_id": run_id, "kind": kind, "text": text, "scope": scope or {},
                                                      "severity": severity, "status": "open"}, client_event_id=client_event_id)

    def set_memo_status(self, reviewer: str, memo_event: dict, status: str, guideline_id: str | None = None) -> dict:
        data = {**memo_event["data"], "status": status}
        if guideline_id:
            data["guideline_id"] = guideline_id
        return self._emit("answer_memo", reviewer, data, supersedes=memo_event["event_id"])

    def add_service_note(self, reviewer: str, service_id: str, service_version: str, kind: str, text: str, *, source_url: str,
                         verified_at: str, expires_at: str, client_event_id: str | None = None) -> dict:
        return self._emit("service_note", reviewer, {"service_id": service_id, "service_version": service_version, "kind": kind, "text": text,
                                                       "source_url": source_url, "verified_at": verified_at, "expires_at": expires_at,
                                                       "status": "active"}, client_event_id=client_event_id)

    def add_guideline(self, reviewer: str, guideline_id: str, text: str, *, status: str = "proposed", applies_to: list[str] | None = None,
                      from_memos: list[str] | None = None, approved_by: str | None = None) -> dict:
        data = {"guideline_id": guideline_id, "text": text, "status": status, "applies_to": applies_to or ["all"],
                "from_memos": from_memos or []}
        if approved_by:
            data["approved_by"] = approved_by
        return self._emit("guideline", reviewer, data)

    # --- 읽기 -------------------------------------------------------------------------------------------------
    def read_events(self, *, strict: bool = False) -> tuple[list[dict], list[dict]]:
        """(이벤트 목록, 오류 목록). strict면 첫 오류에서 예외. 같은 event_id·client_event_id가 겹치면 가장 먼저 쓴 것만 남긴다."""
        events, errors = [], []
        seen_ids, seen_clients = set(), set()
        for path in sorted(self.events_dir.glob("*.jsonl")) if self.events_dir.exists() else []:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    schema.validate_event(event)
                    if event["reviewer"] != path.stem:
                        raise schema.ValidationError("reviewer_file_mismatch")
                except (json.JSONDecodeError, schema.ValidationError) as exc:
                    err = {"file": path.name, "line": number, "error": str(exc)}
                    if strict:
                        raise schema.ValidationError(f"{path.name}:{number}: {exc}") from exc
                    errors.append(err)
                    continue
                client = event.get("client_event_id")
                if event["event_id"] in seen_ids or (client and (event["reviewer"], client) in seen_clients):
                    continue
                seen_ids.add(event["event_id"])
                if client:
                    seen_clients.add((event["reviewer"], client))
                events.append(event)
        events.sort(key=lambda e: (e["ts"], e["event_id"]))
        return events, errors

    def state(self, *, strict: bool = False) -> State:
        events, errors = self.read_events(strict=strict)
        state = State(events, self.adjudicators())
        state.errors = errors
        return state
