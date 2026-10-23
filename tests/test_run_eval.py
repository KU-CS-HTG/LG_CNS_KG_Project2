"""B: eval/run_eval.py. 가짜 검색 함수로 집계를 검증하고, 샘플 색인(가짜 임베딩)으로 끝까지 돌려 본다."""
import json
from pathlib import Path

import pytest

from eval import run_eval
from eval.metrics import TARGETS

TODAY = "2026-10-19"
SAMPLE_QUESTIONS_PATH = Path(__file__).parent / "fixtures" / "sample_eval_questions.jsonl"

QUESTIONS = [
    {"id": "a", "category": "상황형", "question": "qa", "relevant_service_ids": ["S1"]},
    {"id": "b", "category": "상황형", "question": "qb", "relevant_service_ids": ["S2", "S3"]},
    {"id": "c", "category": "제도명", "question": "qc", "relevant_service_ids": ["S9"]},
    {"id": "d", "category": "근거 없음", "question": "qd", "relevant_service_ids": [], "expect_no_answer": True},
    {"id": "e", "category": "상황형", "question": "qe", "relevant_service_ids": [], "todo": "정답 미지정"},
]
RANKINGS = {"qa": ["S1", "X"], "qb": ["X", "S3", "Y"], "qc": ["X", "Y"], "qd": ["X"]}


def fake_search(query, filters, depth):
    return RANKINGS.get(query, []), {"used": True}, False


# --- 질문 분류 -------------------------------------------------------------------
def test_split_questions():
    scored, no_answer, unlabeled = run_eval.split_questions(QUESTIONS)
    assert [q["id"] for q in scored] == ["a", "b", "c"]
    assert [q["id"] for q in no_answer] == ["d"]
    assert [q["id"] for q in unlabeled] == ["e"]


# --- 집계 ------------------------------------------------------------------------
def test_run_experiment_metrics():
    r = run_eval.run_experiment("vector", QUESTIONS, fake_search, k=5, today=TODAY)
    assert r["counts"] == {"scored": 3, "no_answer": 1, "unlabeled": 1}
    m = r["metrics"]
    assert m["hit_rate@5"] == pytest.approx(2 / 3)                 # a, b 적중 / c 실패
    assert m["recall@5"] == pytest.approx((1 + 0.5 + 0) / 3)       # b는 정답 2개 중 1개
    assert m["mrr"] == pytest.approx((1 + 1 / 2 + 0) / 3)
    assert r["no_evidence_rate"] == 0.0                             # 근거 없음 질문인데 결과를 돌려줌
    assert r["unlabeled_ids"] == ["e"]
    assert r["config"]["today"] == TODAY and r["config"]["ks"] == [3, 5, 10]
    assert set(r["targets"]) == set(TARGETS)


def test_run_experiment_per_question_and_category():
    r = run_eval.run_experiment("vector", QUESTIONS, fake_search, k=5)
    by_id = {q["id"]: q for q in r["questions"]}
    assert by_id["a"]["first_hit_rank"] == 1 and by_id["b"]["first_hit_rank"] == 2 and by_id["c"]["first_hit_rank"] is None
    assert r["by_category"]["상황형"]["n"] == 2
    assert r["by_category"]["상황형"]["hit_rate@5"] == 1.0
    assert r["by_category"]["제도명"]["hit_rate@5"] == 0.0


def test_search_depth_covers_largest_k():
    seen = []
    run_eval.run_experiment("vector", QUESTIONS[:1], lambda q, f, depth: (seen.append(depth) or [], {}, False), k=5)
    assert seen == [10]                                              # Hit@10까지 계산하려면 10개를 가져와야 한다


def test_no_answer_question_with_empty_result():
    r = run_eval.run_experiment("hybrid_filter", QUESTIONS, lambda q, f, d: ([], {}, False) if q == "qd" else fake_search(q, f, d))
    assert r["no_evidence_rate"] == 1.0


def test_filters_passed_to_search():
    got = {}
    qs = [{"id": "x", "category": "c", "question": "q", "relevant_service_ids": ["S1"], "filters": {"age": 27}}]
    run_eval.run_experiment("hybrid_filter", qs, lambda q, f, d: (got.update(f) or ["S1"], {}, False))
    assert got == {"age": 27}


def test_no_scored_questions_is_error():
    with pytest.raises(ValueError):
        run_eval.run_experiment("vector", [QUESTIONS[3], QUESTIONS[4]], fake_search)


def test_unknown_experiment():
    with pytest.raises(ValueError):
        run_eval.make_search_fn("rerank")


# --- 출력·저장 -------------------------------------------------------------------
def test_report_and_table():
    r = run_eval.run_experiment("vector", QUESTIONS, fake_search, k=5)
    text = run_eval.format_report(r)
    assert "Hit Rate" in text and "MRR" in text
    assert "정답 미지정 1문항" in text and "e" in text            # 제외된 질문을 알려 준다
    assert "놓친 질문" in text and "[c]" in text                   # 실패한 질문과 정답을 보여 준다
    table = run_eval.comparison_table([r, r])
    assert table.count("\n") == 3 and table.startswith("| 실험 |")


def test_save_result_roundtrip(tmp_path):
    r = run_eval.run_experiment("vector", QUESTIONS, fake_search, k=5)
    path = run_eval.save_result(r, tmp_path / "out")
    assert path.name.startswith("vector_k5_") and path.suffix == ".json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["metrics"]["mrr"] == pytest.approx(r["metrics"]["mrr"])
    assert loaded["questions"][0]["question"] == "qa"               # 한글이 그대로 저장된다


# --- 질문 세트 파일 점검 (팀이 고친 뒤 형식이 깨지지 않았는지) ---------------------
def test_repo_questions_file_is_valid():
    questions = run_eval.load_questions()
    assert questions
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "id가 겹칩니다"
    for q in questions:
        assert {"id", "category", "question", "relevant_service_ids", "expect_no_answer"} <= set(q), q["id"]
        assert set(q.get("filters", {})) <= {"age", "region", "field", "situation", "today"}, q["id"]
        if q["expect_no_answer"]:
            assert not q["relevant_service_ids"], f"{q['id']}: 근거 없음 질문에 정답이 있음"


def test_repo_questions_every_item_has_a_state():
    """질문마다 정답이 있거나, 근거 없음이거나, 정답을 정해야 한다는 todo가 있어야 한다 (방치된 질문 방지)."""
    for q in run_eval.load_questions():
        assert q["relevant_service_ids"] or q["expect_no_answer"] or q.get("todo"), f"{q['id']}: 정답·근거없음·todo 중 하나가 필요"


def test_repo_questions_cover_categories():
    questions = run_eval.load_questions()
    assert len(questions) >= 30                                       # 기획서 9.1: 30개 내외
    cats = {q["category"] for q in questions}
    assert {"상황형", "제도명 아는 질문", "지역·연령 조건", "기한 지난 제도", "근거 없음"} <= cats
    assert any(q["expect_no_answer"] for q in questions)
    assert sum(q["category"] == "상황형" for q in questions) >= len(questions) / 2   # 상황형 중심


# --- 끝까지 (색인 필요) -----------------------------------------------------------
def _write_questions(tmp_path, rows):
    path = tmp_path / "q.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return path


SAMPLE_QUESTIONS = run_eval.load_questions(SAMPLE_QUESTIONS_PATH)


def test_main_all_experiments(indexed_sample, tmp_path, capsys):
    qpath = _write_questions(tmp_path, SAMPLE_QUESTIONS)
    out = tmp_path / "results"
    run_eval.main(["--experiment", "all", "--questions", str(qpath), "--out-dir", str(out), "--today", TODAY])
    text = capsys.readouterr().out
    saved = {p.name.split("_k")[0] for p in out.glob("*.json")}
    assert {"vector", "bm25", "hybrid", "hybrid_filter"} <= saved   # baseline은 구현되면 포함됨
    assert "기획서 9.4" in text and "| hybrid_filter |" in text
    result = json.loads(next(out.glob("hybrid_filter_*.json")).read_text(encoding="utf-8"))
    assert result["counts"] == {"scored": 2, "no_answer": 1, "unlabeled": 1}
    assert result["metrics"]["hit_rate@5"] == 1.0                    # 샘플 데이터에서는 두 질문 모두 적중
    assert result["config"]["today"] == TODAY


def test_main_single_experiment_no_save(indexed_sample, tmp_path, capsys):
    qpath = _write_questions(tmp_path, SAMPLE_QUESTIONS)
    out = tmp_path / "results"
    run_eval.main(["--experiment", "bm25", "--questions", str(qpath), "--out-dir", str(out), "--no-save"])
    assert not out.exists() and "bm25" in capsys.readouterr().out


def test_main_requires_index(fake_embedder, tmp_vectorstore, tmp_path):
    qpath = _write_questions(tmp_path, SAMPLE_QUESTIONS)
    with pytest.raises(SystemExit) as e:
        run_eval.main(["--experiment", "vector", "--questions", str(qpath)])
    assert "색인" in str(e.value)


def test_main_without_labels_explains(indexed_sample, tmp_path):
    qpath = _write_questions(tmp_path, [SAMPLE_QUESTIONS[3]])
    with pytest.raises(SystemExit) as e:
        run_eval.main(["--experiment", "vector", "--questions", str(qpath)])
    assert "정답" in str(e.value)


def test_missing_gold_reports_ids_not_in_index():
    qs = [{"id": "q01", "relevant_service_ids": ["A", "B"]}, {"id": "q02", "relevant_service_ids": ["A"]},
          {"id": "q03", "relevant_service_ids": []}]
    from eval.run_eval import missing_gold
    assert missing_gold(qs, lambda sid: sid == "A") == {"q01": ["B"]}
    assert missing_gold(qs, lambda sid: True) == {}


def test_graded_metrics_use_review_export_fields_and_print_judged_rates():
    qs = [{"id": "g", "category": "상황형", "question": "qg", "relevant_service_ids": ["S1", "S2"], "relevance": {"S1": 2, "S2": 1},
           "hard_negatives": ["X"], "unsure": []}]
    r = run_eval.run_experiment("vector", qs, lambda q, f, d: (["S2", "X", "Z"], {}, False), k=3)
    g = r["graded"]
    assert g["precision@3"] == pytest.approx(1 / 3) and 0 < g["ndcg@3"] < 1
    assert g["bad_rate@3"] == pytest.approx(1 / 3) and g["unjudged_rate@3"] == pytest.approx(1 / 3)
    assert "unjudged" in run_eval.format_report(r)


def test_graded_metrics_without_judgments_are_binary_and_have_no_judged_keys():
    r = run_eval.run_experiment("vector", QUESTIONS, fake_search, k=5)
    assert "precision@5" in r["graded"] and "bad_rate@5" not in r["graded"]
