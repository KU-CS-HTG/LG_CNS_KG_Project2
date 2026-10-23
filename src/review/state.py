"""이벤트 목록 -> 현재 상태. 같은 (질문, 제도, 검수자)의 판정은 가장 최근 것이 이기고, 검수자들의 판정은 합의 규칙으로 합친다."""
from __future__ import annotations

from datetime import date

from src.review import schema


def _latest(events: list[dict]) -> dict:
    return max(events, key=lambda e: (e["ts"], e["event_id"]))


class State:
    def __init__(self, events: list[dict], adjudicators: tuple[str, ...] = ()):
        self.adjudicators = tuple(adjudicators)
        replaced = {e["supersedes"] for e in events if e.get("supersedes")}
        self.events = [e for e in events if e["event_id"] not in replaced]          # 대체된 이벤트는 현재 상태에서 빠진다 (파일에는 남음)

        self.questions: dict[str, dict] = {}
        by_q: dict[str, list[dict]] = {}
        for e in self.events:
            if e["type"] == "question":
                by_q.setdefault(schema.question_id(e["data"]["text"]), []).append(e)
        for qid, evs in by_q.items():
            first, last = min(evs, key=lambda e: (e["ts"], e["event_id"])), _latest(evs)
            data = dict(last["data"])
            data.setdefault("split", schema.default_split(qid))
            self.questions[qid] = {"question_id": qid, "created_by": first["reviewer"], "created_at": first["ts"], "status": "active", **data}
        for e in sorted((e for e in self.events if e["type"] == "question_status"), key=lambda e: (e["ts"], e["event_id"])):
            q = self.questions.get(e["data"]["question_id"])
            if q is not None:
                q["status"] = e["data"]["status"]
                q["status_data"] = e["data"]

        self.runs: dict[str, dict] = {}
        self.latest_run: dict[str, str] = {}
        for e in sorted((e for e in self.events if e["type"] == "run"), key=lambda e: (e["ts"], e["event_id"])):
            self.runs[e["data"]["run_id"]] = {**e["data"], "ts": e["ts"], "reviewer": e["reviewer"]}
            self.latest_run[e["data"]["question_id"]] = e["data"]["run_id"]

        self.judgments: dict[tuple[str, str], dict[str, dict]] = {}                # (qid, sid) -> {reviewer: event}
        grouped: dict[tuple[str, str, str], list[dict]] = {}
        for e in self.events:
            if e["type"] == "judgment":
                d = e["data"]
                grouped.setdefault((d["question_id"], d["service_id"], e["reviewer"]), []).append(e)
        for (qid, sid, reviewer), evs in grouped.items():
            self.judgments.setdefault((qid, sid), {})[reviewer] = _latest(evs)

        self._memo_events = [e for e in self.events if e["type"] == "answer_memo"]
        self._note_events = [e for e in self.events if e["type"] == "service_note"]
        by_g: dict[str, list[dict]] = {}
        for e in self.events:
            if e["type"] == "guideline":
                by_g.setdefault(e["data"]["guideline_id"], []).append(e)
        self._guidelines = {gid: _latest(evs)["data"] for gid, evs in by_g.items()}

    # --- 합의 -------------------------------------------------------------------------------------------------
    def consensus(self, qid: str, sid: str, current_version: str | None = None) -> dict:
        """(질문, 제도)의 최종 판정. status: adjudicated / consensus / conflict / unsure / stale / none.

        - 제도 내용이 바뀐(service_version이 현재와 다른) 판정은 stale로 따로 두고 합의에 쓰지 않는다.
        - 조정자의 판정이 있으면 그것이 이긴다. 사람(human) 판정이 있으면 claude 1차 판정은 무시한다.
        - unsure는 합의에서 빠진다 (다른 사람이 정했으면 그것을 따르고, 전부 unsure면 unsure)."""
        by_reviewer = self.judgments.get((qid, sid), {})
        current, stale = {}, {}
        for reviewer, e in by_reviewer.items():
            fresh = current_version is None or e["data"]["service_version"] == current_version
            (current if fresh else stale)[reviewer] = e
        result = {"status": "none", "label": None, "grade": None, "reviewers": sorted(current), "reasons": [], "stale_reviewers": sorted(stale),
                  "provenance": []}
        if not current:
            result["status"] = "stale" if stale else "none"
            return result
        human = {r: e for r, e in current.items() if e["data"].get("provenance", {"by": "human"})["by"] == "human"}
        pool = human or current
        result["provenance"] = sorted({e["data"].get("provenance", {"by": "human"})["by"] for e in pool.values()})
        decided = {r: e for r, e in pool.items() if e["data"]["label"] != "unsure"}
        if not decided:
            result.update(status="unsure", label="unsure", reasons=sorted({e["data"]["reason"] for e in pool.values()}))
            return result
        adjudicated = {r: e for r, e in decided.items() if r in self.adjudicators}
        if adjudicated:
            winner = _latest(list(adjudicated.values()))
            result.update(status="adjudicated", label=winner["data"]["label"])
        else:
            labels = {e["data"]["label"] for e in decided.values()}
            if len(labels) > 1:
                result.update(status="conflict", labels={r: e["data"]["label"] for r, e in decided.items()})
                return result
            result.update(status="consensus", label=next(iter(labels)))
        result["grade"] = schema.GRADES[result["label"]]
        result["reasons"] = sorted({e["data"]["reason"] for e in decided.values() if e["data"].get("reason")})
        return result

    def judged_services(self, qid: str) -> list[str]:
        return sorted(sid for (q, sid) in self.judgments if q == qid)

    # --- 메모·주석·지침 ---------------------------------------------------------------------------------------------
    def memos(self, *, status: str | None = None, kind: str | None = None) -> list[dict]:
        out = []
        for e in self._memo_events:
            d = e["data"]
            if (status is None or d.get("status", "open") == status) and (kind is None or d["kind"] == kind):
                out.append({**d, "event_id": e["event_id"], "reviewer": e["reviewer"], "ts": e["ts"]})
        return sorted(out, key=lambda m: m["ts"])

    def service_notes(self, service_id: str | None = None, *, today: date | str | None = None, version: str | None = None) -> list[dict]:
        """활성이고 만료되지 않은 주석. version을 주면 제도 내용이 같은 버전에 단 주석만 (내용이 바뀌면 주석은 확인이 필요하다)."""
        today_d = date.fromisoformat(today) if isinstance(today, str) else (today or date.today())
        out = []
        for e in self._note_events:
            d = e["data"]
            if d["status"] != "active" or date.fromisoformat(d["expires_at"]) < today_d:
                continue
            if (service_id is None or d["service_id"] == service_id) and (version is None or d["service_version"] == version):
                out.append({**d, "event_id": e["event_id"], "reviewer": e["reviewer"]})
        return out

    def guidelines(self, *, active: bool = True) -> list[dict]:
        return [g for g in self._guidelines.values() if not active or g["status"] == "active"]

    # --- 통계 -------------------------------------------------------------------------------------------------
    def agreement(self, reviewer_a: str, reviewer_b: str) -> dict:
        """두 검수자가 모두 판정한(unsure 제외) (질문, 제도)의 일치율과 Cohen's kappa."""
        pairs = []
        for (qid, sid), by_r in self.judgments.items():
            if reviewer_a in by_r and reviewer_b in by_r:
                a, b = by_r[reviewer_a]["data"]["label"], by_r[reviewer_b]["data"]["label"]
                if "unsure" not in (a, b):
                    pairs.append((a, b))
        n = len(pairs)
        if n == 0:
            return {"n": 0, "agreement": None, "kappa": None}
        observed = sum(a == b for a, b in pairs) / n
        labels = sorted({x for p in pairs for x in p})
        expected = sum((sum(a == l for a, _ in pairs) / n) * (sum(b == l for _, b in pairs) / n) for l in labels)
        kappa = 1.0 if expected == 1 else (observed - expected) / (1 - expected)
        return {"n": n, "agreement": round(observed, 3), "kappa": round(kappa, 3)}

    def stats(self) -> dict:
        labels, reasons, statuses = {}, {}, {}
        for (qid, sid) in self.judgments:
            c = self.consensus(qid, sid)
            statuses[c["status"]] = statuses.get(c["status"], 0) + 1
            if c["label"]:
                labels[c["label"]] = labels.get(c["label"], 0) + 1
            for r in c["reasons"]:
                reasons[r] = reasons.get(r, 0) + 1
        splits = {}
        for q in self.questions.values():
            splits[q["split"]] = splits.get(q["split"], 0) + 1
        return {"questions": len(self.questions), "by_split": splits, "judged_pairs": len(self.judgments), "consensus_status": statuses,
                "labels": labels, "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
                "memos_open": len(self.memos(status="open")), "guidelines_active": len(self.guidelines())}
