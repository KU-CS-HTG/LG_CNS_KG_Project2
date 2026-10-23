"""eval.rag_compare (일반 LLM vs 우리 서비스) - LLM 없이: 일반 LLM·우리 답변·채점기를 가짜로 바꿔 끼운다."""
from eval import rag_compare
from src.common.schema import Answer, Citation, Recommendation

Q_RENT = {"id": "q01", "question": "월세가 부담돼요", "expected_answer": "월세 지원 제도를 안내한다",
          "relevant_service_names": ["청년월세 한시 특별지원(2차)"], "expect_no_answer": False}
Q_NONE = {"id": "q25", "question": "우주여행 지원금 있나요?", "expected_answer": "근거가 없다고 답한다",
          "relevant_service_names": [], "expect_no_answer": True}


def _ours(question):
    rec = Recommendation("청년월세 한시 특별지원(2차)", "국토교통부", "무주택 청년", "월 최대 20만원", "복지로 신청",
                         needs_check=["소득 — 중위소득 60% 이하"],
                         citations=[Citation("청년월세 한시 특별지원(2차)", "국토교통부", "월 최대 20만원",
                                             "https://www.gov.kr/portal/rcvfvrSvc/dtlEx/1")])
    if "우주" in question:
        return Answer(text="자료에서 찾지 못했어요.", no_evidence=True), {}
    text = ("월세 지원 제도가 있어요.\n1. 청년월세 한시 특별지원(2차) — 국토교통부\n"
            "   - 확인 필요: 소득 — 중위소득 60% 이하\n   - 상세 링크: https://www.gov.kr/portal/rcvfvrSvc/dtlEx/1")
    return Answer(text=text, recommendations=[rec]), {}


def test_compare_scores_both_sides_with_same_grader(monkeypatch):
    graded = []

    def fake_grade(system, user):
        graded.append(user)
        if system == rag_compare.NO_ANSWER_PROMPT:        # 근거 없음 질문: 기대 답변 대신 보류/단정
            assert "[기대하는 답변]" not in user
            return {"verdict": "보류" if "찾지 못했" in user else "단정"}
        return {"verdict": "충족" if "청년월세" in user else "불충족"}

    monkeypatch.setattr(rag_compare, "plain_answer", lambda q: "주거급여를 알아보세요.")   # 정답 제도 이름 없음, 링크 없음
    monkeypatch.setattr(rag_compare.generator, "_answer_with_evidence", _ours)
    monkeypatch.setattr(rag_compare.condition_extractor, "_chat_json", fake_grade)
    result = rag_compare.compare([Q_RENT, Q_NONE])

    assert result["counts"] == {"total": 2, "scored": 2, "errors": 0}
    assert len(graded) == 4                                          # 두 문항 x 두 쪽, 같은 채점기
    ours, plain = result["metrics"]["ours"], result["metrics"]["plain"]
    assert ours["answer_correctness"] == 1.0 and plain["answer_correctness"] == 0.0
    assert ours["service_hit"] == 1.0 and plain["service_hit"] == 0.0   # 근거 없음 문항은 평균에서 빠진다
    assert ours["link_rate"] == 0.5 and plain["link_rate"] == 0.0
    assert result["questions"][0]["ours_text"].count("확인 필요") == 1  # answer.text 그대로 (카드 내용을 다시 붙이지 않는다)
    assert "일반 LLM" in rag_compare.format_report(result)


def test_name_match_ignores_spaces_and_parenthesized_suffix():
    assert rag_compare.name_match("청년 월세 한시 특별지원을 신청하세요", Q_RENT)["service_hit"] == 1.0
    assert rag_compare.name_match("월세 지원", Q_RENT)["service_hit"] == 0.0
    assert rag_compare.name_match("아무 말", Q_NONE) == {}


def test_one_failure_does_not_stop_the_run(monkeypatch):
    def boom(q):
        raise TimeoutError("network")
    monkeypatch.setattr(rag_compare, "plain_answer", boom)
    result = rag_compare.compare([Q_RENT])
    assert result["counts"]["errors"] == 1 and "TimeoutError" in rag_compare.format_report(result)


def test_demo_mode_skips_grading_and_writes_side_by_side(monkeypatch, tmp_path):
    def no_grader(system, user):
        raise AssertionError("시연 모드는 채점하지 않는다")
    monkeypatch.setattr(rag_compare, "plain_answer", lambda q: "청년구직활동지원금을 신청하세요.")
    monkeypatch.setattr(rag_compare.generator, "_answer_with_evidence", _ours)
    monkeypatch.setattr(rag_compare.condition_extractor, "_chat_json", no_grader)
    monkeypatch.setattr(rag_compare.config, "OPENAI_API_KEY", "test")
    demo = tmp_path / "demo.jsonl"
    demo.write_text('{"id": "d1", "question": "월세가 부담돼요", "shows": "지역 제도"}\n', encoding="utf-8")
    rag_compare.main(["--demo", "--questions", str(demo), "--out-dir", str(tmp_path)])
    md = next(tmp_path.glob("demo_*.md")).read_text(encoding="utf-8")
    assert "보여 줄 것: 지역 제도" in md and "청년구직활동지원금" in md and "청년월세 한시 특별지원(2차)" in md
