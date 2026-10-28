"""답변 평가 비용 줄이기: --only로 질문 고르기, --no-judge로 LLM 채점 끄기 (eval/answer_eval.py). LLM·색인 없이."""
import pytest

from eval import answer_eval
from src.common.schema import Answer, Chunk, Citation, Recommendation
from src.generate import condition_extractor, generator

QUESTIONS = [{"id": "q01", "question": "월세가 부담돼요", "relevant_service_ids": ["S1"], "expected_answer": "월세 지원 안내"},
             {"id": "q02", "question": "청년도약계좌 가입 조건", "expect_no_answer": True},
             {"id": "q03", "question": "면접 교통비", "relevant_service_ids": ["S9"], "expected_answer": "면접 지원"}]
CHUNK = Chunk(chunk_id="S1#지원내용", service_id="S1", service_name="월세 지원", section="지원내용", text="월세 지원 월 20만원")
REC = Recommendation(service_name="월세 지원", agency="국토교통부", reason="이유", benefit="월 20만원", how_to_apply="온라인",
                     citations=[Citation(service_name="월세 지원", agency="국토교통부", quote="월 20만원", detail_url="https://x")],
                     service_id="S1")


@pytest.fixture
def no_llm(monkeypatch):
    calls = []
    monkeypatch.setattr(condition_extractor, "_chat_json", lambda *a: calls.append(a) or {"verdict": "충족", "verdicts": []})

    def answer(question):
        if "도약계좌" in question:
            return Answer(text="근거 없음", recommendations=[], no_evidence=True), {}
        return Answer(text="안내", recommendations=[REC]), {"S1": [CHUNK]}
    monkeypatch.setattr(generator, "_answer_with_evidence", answer)
    return calls


def test_only_picks_questions_in_set_order_and_limit_still_applies():
    assert [q["id"] for q in answer_eval.select_questions(QUESTIONS, "q03, q01")] == ["q01", "q03"]
    assert [q["id"] for q in answer_eval.select_questions(QUESTIONS, "q03,q01", 1)] == ["q01"]
    assert [q["id"] for q in answer_eval.select_questions(QUESTIONS, None, 2)] == ["q01", "q02"]


def test_no_judge_makes_no_grading_calls_and_keeps_code_metrics(no_llm):
    result = answer_eval.evaluate(QUESTIONS, indexed=lambda sid: True, judge=False)
    assert no_llm == []                                                  # 채점 LLM 호출 없음
    m = result["metrics"]
    assert m["answer_correctness"] is None and m["faithfulness"] is None
    assert m["no_answer_correctness"] == 1.0                             # 근거 없음 질문은 코드 판정이라 그대로
    assert (m["service_hit"], m["citation_accuracy"]) == (0.5, 1.0)       # q01 맞힘, q03 놓침
    report = answer_eval.format_report(result)
    assert "LLM 채점 끔" in report and "q03" in report


def test_judge_on_is_unchanged(no_llm):
    result = answer_eval.evaluate(QUESTIONS[:1], indexed=lambda sid: True)
    assert len(no_llm) == 2                                              # Correctness 1번 + 제도 1개 Faithfulness 1번
    assert result["metrics"]["answer_correctness"] == 1.0 and result["config"]["judge"] is True
