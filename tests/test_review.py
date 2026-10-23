"""src/review: 스키마 검증·저장·합의·시험지 내보내기·대기열·기존 질문 이전. 임시 폴더와 가짜 제도 데이터만 쓴다."""
import json
import sqlite3
from datetime import date

import pytest

from src.review import cli, legacy, schema
from src.review.export import build_rows, export_eval
from src.review.queue import build_queue
from src.review.state import State
from src.review.store import ReviewStore

TODAY = "2026-10-23"


def svc(sid, name="청년 월세 지원", deadline="상시신청", target="청년", content="월세 지원"):
    return {"service_id": sid, "service_name": name, "support_target": target, "selection_criteria": "", "support_content": content,
            "how_to_apply": "온라인 신청", "application_period": deadline}


SERVICES = {s: svc(s) for s in ("S1", "S2", "S3", "S4")}
SERVICES["EXP"] = svc("EXP", deadline="2025.01.01 ~ 2025.01.31")          # 기한 지남


def ver(sid, services=SERVICES):
    return schema.service_version(services[sid])


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "reviews"
    (root / "events").mkdir(parents=True)
    (root / "reviewers.json").write_text(json.dumps({"adjudicators": ["taegyu"]}), encoding="utf-8")
    return ReviewStore(root)


@pytest.fixture
def q(store):
    return store.add_question("taegyu", "월세가 부담되고 취업을 준비하고 있어요", split="dev", category="상황형")


# --- 스키마 ------------------------------------------------------------------------------------------------
def test_question_id_is_stable_and_normalized():
    assert schema.question_id("월세가  부담돼요 ") == schema.question_id("월세가 부담돼요") and schema.question_id("a").startswith("q_")
    assert schema.question_id("월세가 부담돼요") != schema.question_id("월세가 힘들어요")


def test_default_split_is_deterministic_and_roughly_8_to_2():
    splits = [schema.default_split(schema.question_id(f"질문 {i}")) for i in range(1000)]
    assert splits == [schema.default_split(schema.question_id(f"질문 {i}")) for i in range(1000)]
    assert 0.74 < splits.count("dev") / 1000 < 0.86


def test_service_version_covers_six_fields_only():
    base = svc("S1")
    assert schema.service_version(base) == schema.service_version({**base, "support_target": " 청년 "}) == schema.service_version({**base, "region": "서울", "agency": "x"})
    for field in schema.VERSION_FIELDS:
        assert schema.service_version({**base, field: base[field] + " 변경"}) != schema.service_version(base), field   # 기한도 포함
    assert schema.service_version(None) == schema.UNKNOWN_VERSION


@pytest.mark.parametrize("label,reason,ok", [("good", None, True), ("good", "irrelevant", False), ("bad", None, False), ("bad", "irrelevant", True),
                                             ("partial", "not_eligible", True), ("partial", "zzz", False), ("unsure", "needs_source_check", True),
                                             ("unsure", "irrelevant", False), ("unsure", None, False), ("maybe", None, False),
                                             ("bad", "other", False)])
def test_judgment_label_reason_rules(label, reason, ok):
    data = {"question_id": "q_1", "service_id": "S1", "service_version": "t_x", "label": label, "reason": reason}
    if ok:
        schema.make_event("judgment", data, "a")
    else:
        with pytest.raises(schema.ValidationError):
            schema.make_event("judgment", data, "a")
    schema.make_event("judgment", {**data, "label": "bad", "reason": "other", "note": "설명"}, "a")          # other는 note 필수


def test_other_event_rules():
    with pytest.raises(schema.ValidationError, match="invalid_reviewer"):
        schema.make_event("guideline", {"guideline_id": "g", "text": "t", "status": "proposed"}, "bad name!")
    with pytest.raises(schema.ValidationError, match="approved_by"):
        schema.make_event("guideline", {"guideline_id": "g", "text": "t", "status": "active"}, "a")
    with pytest.raises(schema.ValidationError, match="paraphrase_of"):
        schema.make_event("question", {"text": "질문", "source": "paraphrase"}, "a")
    with pytest.raises(schema.ValidationError, match="invalid_source"):
        schema.make_event("question", {"text": "질문", "source": "real_user"}, "a")                    # 실사용 질문은 받지 않는다
    with pytest.raises(schema.ValidationError, match="question_id_mismatch"):
        schema.make_event("question", {"text": "질문", "source": "team", "question_id": "q_00000000"}, "a")
    with pytest.raises(schema.ValidationError, match="no_answer_requires_checks"):
        schema.make_event("question_status", {"question_id": "q_1", "status": "no_answer", "checks": ["하나", "둘"]}, "a")
    schema.make_event("question_status", {"question_id": "q_1", "status": "no_answer", "checks": ["하나", "둘", "셋"]}, "a")
    note = {"service_id": "S1", "service_version": "t_x", "kind": "caution", "text": "t", "source_url": "https://x.kr", "verified_at": "2026-10-01",
            "expires_at": "2026-12-01", "status": "active"}
    schema.make_event("service_note", note, "a")
    for bad in ({"source_url": "x.kr"}, {"expires_at": "2026-09-01"}, {"kind": "zzz"}):
        with pytest.raises(schema.ValidationError):
            schema.make_event("service_note", {**note, **bad}, "a")
    with pytest.raises(schema.ValidationError, match="guideline_id"):
        schema.make_event("answer_memo", {"run_id": "r", "kind": "rule", "text": "t", "status": "promoted"}, "a")


# --- 저장 ------------------------------------------------------------------------------------------------
def test_append_per_reviewer_file_dedupes_client_event_id(store, q):
    store.add_judgment("minkyu", q, "S1", ver("S1"), "good", client_event_id="c1")
    assert store.last_written
    store.add_judgment("minkyu", q, "S1", ver("S1"), "good", client_event_id="c1")        # 화면이 다시 실행돼도 한 번만
    assert not store.last_written
    assert {p.name for p in store.events_dir.glob("*.jsonl")} == {"taegyu.jsonl", "minkyu.jsonl"}
    assert len(store.read_events()[0]) == 2


def test_append_recovers_from_truncated_last_line(store, q):
    path = store.events_dir / "taegyu.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + '{"event_id": "broken', encoding="utf-8")   # 쓰다가 끊긴 줄
    store.add_judgment("taegyu", q, "S1", ver("S1"), "good")
    events, errors = store.read_events()
    assert len(events) == 2 and len(errors) == 1 and errors[0]["line"] == 2


def test_read_events_reports_errors_and_strict_raises(store, q):
    (store.events_dir / "a.jsonl").write_text(json.dumps(schema.make_event("judgment", {"question_id": q, "service_id": "S1", "service_version": "t",
                                                                                          "label": "good"}, "someone_else")) + "\n", encoding="utf-8")
    events, errors = store.read_events()
    assert errors[0]["error"] == "reviewer_file_mismatch" and len(events) == 1
    with pytest.raises(schema.ValidationError, match="a.jsonl:1"):
        store.read_events(strict=True)


# --- 합의 ------------------------------------------------------------------------------------------------
def consensus(store, q, sid="S1", services=SERVICES):
    return store.state().consensus(q, sid, ver(sid, services))


def test_consensus_rules(store, q):
    assert consensus(store, q)["status"] == "none"
    store.add_judgment("a", q, "S1", ver("S1"), "good")
    assert consensus(store, q)["status"] == "consensus" and consensus(store, q)["grade"] == 2
    store.add_judgment("b", q, "S1", ver("S1"), "good")
    assert consensus(store, q)["reviewers"] == ["a", "b"]
    store.add_judgment("c", q, "S1", ver("S1"), "bad", reason="irrelevant")
    c = consensus(store, q)
    assert c["status"] == "conflict" and c["labels"] == {"a": "good", "b": "good", "c": "bad"}
    store.add_judgment("taegyu", q, "S1", ver("S1"), "partial", reason="not_eligible")        # 조정자가 이긴다
    c = consensus(store, q)
    assert c["status"] == "adjudicated" and c["label"] == "partial" and c["grade"] == 1


def test_unsure_is_excluded_from_agreement_and_human_beats_claude(store, q):
    store.add_judgment("a", q, "S2", ver("S2"), "unsure", reason="needs_source_check")
    assert consensus(store, q, "S2")["status"] == "unsure"
    store.add_judgment("b", q, "S2", ver("S2"), "good")
    assert consensus(store, q, "S2")["status"] == "consensus" and consensus(store, q, "S2")["label"] == "good"
    store.add_judgment("legacy", q, "S3", ver("S3"), "good", by="claude")
    assert consensus(store, q, "S3")["provenance"] == ["claude"]
    store.add_judgment("a", q, "S3", ver("S3"), "bad", reason="irrelevant")                      # 사람 판정이 claude 1차를 대체
    c = consensus(store, q, "S3")
    assert c["label"] == "bad" and c["provenance"] == ["human"]


def test_same_reviewer_latest_wins_and_supersedes(store, q):
    first = store.add_judgment("a", q, "S1", ver("S1"), "bad", reason="irrelevant")
    store.add_judgment("a", q, "S1", ver("S1"), "good")
    assert consensus(store, q)["label"] == "good"
    memo = store.add_memo("a", "r_1", "error", "금액이 틀림")
    store.set_memo_status("a", memo, "accepted")
    assert [m["status"] for m in store.state().memos()] == ["accepted"]                        # 이전 상태는 대체돼 한 건만
    assert first["event_id"] and len(store.read_events()[0]) >= 4


def test_stale_when_service_text_or_deadline_changes(store, q):
    store.add_judgment("a", q, "S1", ver("S1"), "good")
    changed = {**SERVICES, "S1": svc("S1", deadline="2026.12.31까지")}
    c = store.state().consensus(q, "S1", schema.service_version(changed["S1"]))
    assert c["status"] == "stale" and c["stale_reviewers"] == ["a"] and c["label"] is None
    unchanged_meta = {**SERVICES["S1"], "region": "서울", "agency": "서울시"}
    assert store.state().consensus(q, "S1", schema.service_version(unchanged_meta))["status"] == "consensus"


def test_agreement_kappa(store):
    qs = [store.add_question("a", f"질문 {i}") for i in range(4)]
    for i, qid in enumerate(qs):
        store.add_judgment("a", qid, "S1", ver("S1"), "good" if i < 2 else "bad", reason=None if i < 2 else "irrelevant")
        b_label = "good" if i in (0, 2) else "bad"
        store.add_judgment("b", qid, "S1", ver("S1"), b_label, reason=None if b_label == "good" else "irrelevant")
    out = store.state().agreement("a", "b")
    assert out["n"] == 4 and out["agreement"] == 0.5 and out["kappa"] == 0.0
    assert store.state().agreement("a", "zzz")["n"] == 0


def test_notes_expire_and_follow_service_version(store):
    store.add_service_note("a", "S1", ver("S1"), "deadline_info", "상반기 모집", source_url="https://x.kr", verified_at="2026-10-01", expires_at="2026-11-30")
    st = store.state()
    assert len(st.service_notes("S1", today="2026-10-23")) == 1
    assert st.service_notes("S1", today="2026-12-01") == []                                    # 만료
    assert st.service_notes("S1", today="2026-10-23", version="t_other") == []                # 제도 내용이 바뀌면 주석은 확인이 필요
    store.add_guideline("a", "g1", "신청 기한을 맨 위에", status="active", approved_by="taegyu")
    store.add_guideline("a", "g2", "초안", status="proposed")
    assert [g["guideline_id"] for g in store.state().guidelines()] == ["g1"]


# --- 내보내기 ----------------------------------------------------------------------------------------------
def test_export_rows(store, q):
    q2 = store.add_question("taegyu", "공개 시험용 질문", split="test", gold_filters={"age": 27, "region": "서울"})
    store.add_judgment("a", q, "S1", ver("S1"), "good")
    store.add_judgment("a", q, "S2", ver("S2"), "partial", reason="not_eligible")
    store.add_judgment("a", q, "S3", ver("S3"), "bad", reason="generic_noise")
    store.add_judgment("a", q, "S4", ver("S4"), "unsure", reason="needs_discussion")
    store.add_judgment("a", q, "EXP", ver("EXP"), "good")                                       # 기한 지난 제도는 자동 제외
    store.add_judgment("a", q, "GONE", "t_x", "good")                                           # 색인(활성)에 없는 제도
    store.add_run("a", q, [{"service_id": s, "rank": r, "source": "hybrid_filter"} for r, s in enumerate(["S1", "S2", "S3", "S4", "EXP"], 1)], system={"git": "x"})
    rows, report = build_rows(store.state(), split="dev", today=TODAY, services=SERVICES)
    assert len(rows) == 1
    row = rows[0]
    assert row["relevant_service_ids"] == ["S1"] and row["relevance"] == {"S1": 2, "S2": 1} and row["hard_negatives"] == ["S3"]
    assert row["unsure"] == ["S4"] and row["label_sources"] == ["human"] and row["split"] == "dev" and not row["expect_no_answer"]
    assert row["judged_coverage_at_10"] == 1.0                                                  # 기한 지난 EXP는 분모에서 제외
    assert report["expired_excluded"] == 1 and report["missing_services"] == 1 and report["with_relevant"] == 1
    test_rows, _ = build_rows(store.state(), split="test", today=TODAY, services=SERVICES)
    assert [r["question_id"] for r in test_rows] == [q2] and test_rows[0]["filters"] == {"age": 27, "region": "서울"}
    assert test_rows[0]["relevant_service_ids"] == []
    # 기한이 열려 있는 날짜로 보면 EXP가 다시 들어온다
    rows_old, _ = build_rows(store.state(), split="dev", today="2025-01-15", services=SERVICES)
    assert rows_old[0]["relevant_service_ids"] == ["EXP", "S1"]


def test_export_marks_stale_conflict_no_answer_and_freezes_test(store, q, tmp_path):
    store.add_judgment("a", q, "S1", ver("S1"), "good")
    store.add_judgment("b", q, "S1", ver("S1"), "bad", reason="irrelevant")
    store.add_judgment("a", q, "S2", ver("S2"), "good")
    changed = {**SERVICES, "S2": svc("S2", content="완전히 다른 내용")}
    rows, report = build_rows(store.state(), split="dev", today=TODAY, services=changed)
    assert rows[0]["conflicts"] == ["S1"] and rows[0]["stale"] == ["S2"] and rows[0]["relevant_service_ids"] == []
    assert report["stale"] == 1 and report["conflicts"] == [f"{q}:S1"]
    qn = store.add_question("taegyu", "범위 밖 질문", split="dev")
    store.set_status("taegyu", qn, "no_answer", checks=["검색1", "검색2", "검색3"])
    rows, report = build_rows(store.state(), split="dev", today=TODAY, services=SERVICES)
    assert [r["expect_no_answer"] for r in rows if r["question_id"] == qn] == [True] and report["no_answer"] == 1
    store.set_status("taegyu", qn, "retired")
    assert qn not in {r["question_id"] for r in build_rows(store.state(), split="dev", today=TODAY, services=SERVICES)[0]}
    out = tmp_path / "x" / "test_v1.jsonl"
    export_eval(store.state(), split="test", today=TODAY, services=SERVICES, path=out)
    with pytest.raises(FileExistsError, match="frozen_export_exists"):
        export_eval(store.state(), split="test", today=TODAY, services=SERVICES, path=out, overwrite=True)    # test는 덮어쓸 수 없다
    dev = tmp_path / "dev.jsonl"
    export_eval(store.state(), split="dev", today=TODAY, services=SERVICES, path=dev)
    with pytest.raises(FileExistsError):
        export_eval(store.state(), split="dev", today=TODAY, services=SERVICES, path=dev)
    export_eval(store.state(), split="dev", today=TODAY, services=SERVICES, path=dev, overwrite=True)


def test_exported_file_is_readable_by_run_eval(store, q, tmp_path):
    from eval import run_eval

    store.add_judgment("a", q, "S1", ver("S1"), "good")
    qn = store.add_question("taegyu", "범위 밖 질문", split="dev")
    store.set_status("taegyu", qn, "no_answer", checks=["1", "2", "3"])
    store.add_question("taegyu", "정답 미정 질문", split="dev")
    path = tmp_path / "dev.jsonl"
    export_eval(store.state(), split="dev", today=TODAY, services=SERVICES, path=path)
    scored, no_answer, unlabeled = run_eval.split_questions(run_eval.load_questions(str(path)))
    assert len(scored) == 1 and len(no_answer) == 1 and len(unlabeled) == 1


# --- 대기열 ----------------------------------------------------------------------------------------------
def test_queue_new_changed_expired_and_lists(store, q):
    store.add_judgment("a", q, "S1", ver("S1"), "good")                       # 덮인 후보
    store.add_judgment("a", q, "S2", ver("S2"), "good")                       # 내용이 바뀜 -> changed
    store.add_judgment("a", q, "S3", ver("S3"), "unsure", reason="needs_source_check")
    store.add_judgment("a", q, "S4", ver("S4"), "good")
    store.add_judgment("b", q, "S4", ver("S4"), "bad", reason="irrelevant")   # 충돌
    services = {**SERVICES, "S2": svc("S2", content="바뀐 내용"), "NEW": svc("NEW", name="신규 제도")}

    def search(text, filters, n):
        return [("S1", 1, "hybrid_filter"), ("NEW", 2, "vector"), ("S2", 3, "bm25"), ("EXP", 4, "vector"), ("S3", 5, "bm25"), ("GONE", 6, "bm25"),
                ("NEW", 9, "bm25")]

    result = build_queue(store.state(), search, services, today=TODAY, n=30)
    assert [(i["service_id"], i["kind"], i["rank"], i["source"]) for i in result["items"]] == [("S2", "changed", 3, "bm25"), ("NEW", "new", 2, "vector")]   # changed가 먼저, 같은 제도는 최고 순위만
    assert result["skipped"] == {"covered": 2, "expired": 1, "inactive": 1}
    assert [u["service_id"] for u in result["unsure"]] == ["S3"] and [c["service_id"] for c in result["conflicts"]] == ["S4"]
    assert build_queue(store.state(), search, services, today=TODAY, split="test")["items"] == []       # split 필터


# --- 기존 질문 이전 ----------------------------------------------------------------------------------------
LEGACY = [
    {"id": "q01", "category": "상황형", "topic": "주거", "question": "월세가 부담돼요", "relevant_service_ids": ["S1", "S2"], "expected_answer": "x",
     "expect_no_answer": False, "label_source": "claude", "reviewed": False},
    {"id": "q02", "category": "지역·연령 조건", "topic": "주거", "question": "서울 27살 월세", "relevant_service_ids": ["S3"], "expect_no_answer": False,
     "filters": {"age": 27, "region": "서울"}, "label_source": "claude", "reviewed": True},
    {"id": "q03", "category": "근거 없음", "topic": "x", "question": "기초연금은?", "relevant_service_ids": [], "expect_no_answer": True},
    {"id": "q04", "category": "지역 후속 질문", "topic": "주거", "question": "경기에 살아요. 월세가 부담돼요", "relevant_service_ids": ["S4"], "expect_no_answer": False,
     "follow_up_of": "q01", "filters": {"region": "경기"}},
]


def test_legacy_import_is_idempotent_and_keeps_provenance(store, tmp_path):
    path = tmp_path / "questions.jsonl"
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in LEGACY), encoding="utf-8")
    first = legacy.import_legacy(store, path, "legacy", SERVICES)
    assert first == {"questions": 4, "judgments": 4, "no_answer": 1, "skipped_existing": 0, "unknown_services": 0}
    again = legacy.import_legacy(store, path, "legacy", SERVICES)
    assert again["questions"] == 0 and again["judgments"] == 0 and again["skipped_existing"] == 4
    state = store.state()
    assert {q["split"] for q in state.questions.values()} == {"dev"}                         # 이미 튜닝에 쓴 질문은 모두 dev
    q1, q2 = schema.question_id("월세가 부담돼요"), schema.question_id("서울 27살 월세")
    assert state.consensus(q1, "S1", ver("S1"))["provenance"] == ["claude"] and state.consensus(q2, "S3", ver("S3"))["provenance"] == ["human"]
    assert state.questions[q2]["gold_filters"] == {"age": 27, "region": "서울"}
    assert state.questions[schema.question_id("기초연금은?")]["status"] == "no_answer"
    rows, _ = build_rows(state, split="dev", today=TODAY, services=SERVICES)
    assert {r["id"]: r["relevant_service_ids"] for r in rows} == {"q01": ["S1", "S2"], "q02": ["S3"], "q03": [], "q04": ["S4"]}
    assert next(r for r in rows if r["id"] == "q03")["expect_no_answer"] is True


# --- CLI ------------------------------------------------------------------------------------------------
def make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE services (service_id TEXT PRIMARY KEY, payload TEXT, status TEXT)")
    for sid, s in SERVICES.items():
        con.execute("INSERT INTO services VALUES (?,?,?)", (sid, json.dumps(s, ensure_ascii=False), "active"))
    con.execute("INSERT INTO services VALUES ('OFF', '{}', 'inactive')")
    con.commit()
    con.close()


def test_cli_import_validate_stats_export(tmp_path, capsys):
    db, qs, root = tmp_path / "services.db", tmp_path / "questions.jsonl", tmp_path / "reviews"
    make_db(db)
    qs.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in LEGACY), encoding="utf-8")
    base = ["--root", str(root)]
    assert set(cli.load_services(db)) == set(SERVICES)                                      # inactive는 제외
    assert cli.main([*base, "import-legacy", "--questions", str(qs), "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["questions"] == 4
    assert cli.main([*base, "validate"]) == 0 and "문제 없음" in capsys.readouterr().out
    assert cli.main([*base, "stats"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["questions"] == 4 and stats["labels"]["good"] == 4
    out = tmp_path / "dev.jsonl"
    assert cli.main([*base, "export", "--split", "dev", "--out", str(out), "--db", str(db), "--today", TODAY]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 4
    assert cli.main([*base, "export", "--split", "dev", "--out", str(out), "--db", str(db)]) == 1      # 덮어쓰기 거부
    (root / "events" / "legacy.jsonl").write_text("not json\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main([*base, "validate"]) == 1 and "legacy.jsonl:1" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.load_services(tmp_path / "없음.db")


def test_same_instant_edits_keep_write_order(store, q):
    """같은 시각(ts)에 쓴 이벤트는 쓴 순서로 정해진다 (이전에는 event_id 순이라 같은 초의 수정이 뒤바뀔 수 있었다)."""
    for i in range(30):
        qid = store.add_question("a", f"순서 시험 {i}")
        stamp = "2026-10-23T09:00:00.000000Z"
        first = schema.make_event("judgment", {"question_id": qid, "service_id": "S1", "service_version": ver("S1"), "label": "bad", "reason": "irrelevant"},
                                  "a", ts=stamp)
        second = schema.make_event("judgment", {"question_id": qid, "service_id": "S1", "service_version": ver("S1"), "label": "good"}, "a", ts=stamp)
        store.append(first)
        store.append(second)
        assert consensus(store, qid)["label"] == "good"                                   # 나중에 쓴 판정이 이긴다
    assert schema.now_iso().endswith("Z") and "." in schema.now_iso()                    # 마이크로초까지


# --- import-questions --------------------------------------------------------------------------------------------
def test_import_questions_csv_with_korean_headers_and_bom(store, tmp_path):
    f = tmp_path / "q.csv"
    f.write_text("﻿질문,범주,나이,지역,태그\n월세 지원 알려줘,상황형,27,서울,주거;청년\n취업 준비 중이에요,상황형,,,\n", encoding="utf-8")
    report = cli_import(store, f)
    assert report["added"] == 2 and not report["invalid"] and sum(report["by_split"].values()) == 2
    q = store.state().questions[schema.question_id("월세 지원 알려줘")]
    assert q["gold_filters"] == {"region": "서울", "age": 27} and q["tags"] == ["주거", "청년"] and q["source"] == "team"


def test_import_questions_is_idempotent_and_skips_duplicates(store, tmp_path):
    store.add_question("taegyu", "이미 있는 질문")
    f = tmp_path / "q.jsonl"
    f.write_text("\n".join(json.dumps({"question": t}, ensure_ascii=False) for t in ("이미  있는 질문", "새 질문", "새 질문")), encoding="utf-8")
    first = cli_import(store, f)
    assert (first["added"], first["existing"], first["duplicate_in_file"]) == (1, 1, 1)
    assert cli_import(store, f)["added"] == 0


def test_import_questions_reports_bad_rows_and_dry_run_writes_nothing(store, tmp_path):
    f = tmp_path / "q.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in (
        {"question": "정상 질문"}, {"question": ""}, {"question": "나이 이상", "age": "abc"},
        {"question": "출처 이상", "source": "real_user"}, {"question": "패러프레이즈", "source": "paraphrase"})), encoding="utf-8")
    report = cli_import(store, f, dry_run=True)
    assert report["added"] == 1 and len(report["invalid"]) == 4 and not store.state().questions


def cli_import(store, path, dry_run=False):
    from src.review import intake
    return intake.import_questions(store, path, "taegyu", dry_run=dry_run)
