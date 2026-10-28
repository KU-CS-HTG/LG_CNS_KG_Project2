"""검색 상위 K개 나쁨 판정의 사유별 집계 (eval/bad_reasons.py, WORK_ITEMS R1 1단계). 색인·검수 저장소 없이 가짜 판정으로."""
from eval import bad_reasons, run_eval

REASONS = {("qa", "B1"): ["irrelevant"], ("qa", "B2"): ["wrong_region"], ("qb", "B3"): ["wrong_region", "not_eligible"],
           ("qb", "B4"): []}


def consensus(qid, sid):
    return {"reasons": REASONS.get((qid, sid), [])}


def region(question):
    return "서울" if "서울" in question else None


ROWS = [
    {"id": "q1", "question_id": "qa", "question": "월세가 부담돼요", "retrieved": ["G1", "B1", "B2", "G2", "G3", "B9"],
     "hard_negatives": ["B1", "B2", "B9"]},                            # B9는 6위라 K=5에서 세지 않는다
    {"id": "q2", "question_id": "qb", "question": "서울 사는 25살인데 월세", "retrieved": ["B3", "B4", "G4", "G5", "G6"],
     "hard_negatives": ["B3", "B4"]},
    {"id": "q3", "question": "검수 시험지가 아닌 질문", "retrieved": ["X1"]},   # hard_negatives 없음: 세지 않는다
]


def test_counts_bad_in_top_k_by_reason_and_splits_wrong_region():
    s = bad_reasons.summarize(ROWS, 5, consensus, region, describe=lambda sid: f"제도 {sid}")
    assert (s["results"], s["bad"]) == (10, 4)
    assert s["by_reason"] == {"wrong_region": 2, "irrelevant": 1, "not_eligible": 1, bad_reasons.NO_REASON: 1}
    assert s["wrong_region"] == {"question_region": 1, "no_region": 1}
    [case] = s["examples"]["irrelevant"]
    assert (case["id"], case["rank"], case["service"]) == ("q1", 2, "제도 B1")


def test_report_lists_reasons_region_split_and_examples():
    text = bad_reasons.format_summary(bad_reasons.summarize(ROWS, 5, consensus, region))
    assert "나쁨 판정 4건 / 결과 10건" in text
    assert "질문에 지역 있음(다른 지역 제도) 1건" in text and "질문에 지역 없음(지자체 제도가 올라옴) 1건" in text
    assert "[q2] (서울)" in text and "[q1] (지역 없음)" in text


def test_run_eval_report_includes_summary_when_present():
    result = {"experiment": "hybrid_filter", "config": {"k": 5, "ks": [5]}, "counts": {"scored": 1, "no_answer": 0, "unlabeled": 0},
              "metrics": {"hit_rate@5": 1.0, "recall@5": 1.0, "mrr": 1.0}, "graded": {}, "targets": {}, "no_evidence_rate": None,
              "by_category": {}, "questions": [], "bad_reasons": bad_reasons.summarize(ROWS, 5, consensus, region)}
    assert "사유별" in run_eval.format_report(result)
    result.pop("bad_reasons")
    assert "사유별" not in run_eval.format_report(result)


def test_run_experiment_keeps_question_id_for_review_lookup():
    q = {"id": "q1", "question_id": "qa", "question": "월세", "relevant_service_ids": ["G1"], "relevance": {"G1": 2},
         "hard_negatives": ["B1"]}
    result = run_eval.run_experiment("hybrid_filter", [q], lambda text, filters, depth: (["G1", "B1"], {}, False), k=5)
    assert result["questions"][0]["question_id"] == "qa"
