"""이어진 질문을 판정 기준 문서로 보낼지 판단 (src/generate/guide_router.py)."""
import json
from pathlib import Path

import pytest

from src.common import config
from src.common.schema import Recommendation
from src.generate import condition_extractor as ce
from src.generate import guide_router

RECS = [Recommendation(service_name="주거급여 (맞춤형 급여)", agency="국토교통부", reason="", benefit="",
                       how_to_apply="", service_id="336000000096"),
        Recommendation(service_name="생계급여", agency="보건복지부", reason="", benefit="", how_to_apply="",
                       service_id="WII000001410")]


def test_guide_kind_goes_to_guide(monkeypatch):
    seen = {}
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: seen.update(user=u) or {"kind": "guide"})
    assert guide_router.is_guide_question("부모님과 따로 살면 같은 가구예요?", RECS)
    assert "주거급여 (맞춤형 급여)" in seen["user"] and "생계급여" in seen["user"]   # 안내받은 제도를 함께 준다


def test_search_kind_or_unknown_keeps_current_flow(monkeypatch):
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {"kind": "search"})
    assert not guide_router.is_guide_question("소득은 월 150만 원이에요", RECS)
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {"answer": "guide"})
    assert not guide_router.is_guide_question("소득은 월 150만 원이에요", RECS)


def test_llm_failure_keeps_current_flow(monkeypatch):
    def boom(s, u):
        raise RuntimeError("network")
    monkeypatch.setattr(ce, "_chat_json", boom)
    assert not guide_router.is_guide_question("부모님과 따로 살면 같은 가구예요?", RECS)


def test_no_recommendations_or_empty_question_skip_llm(monkeypatch):
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: pytest.fail("LLM을 부르지 않는다"))
    assert not guide_router.is_guide_question("부모님과 따로 살면 같은 가구예요?", [])
    assert not guide_router.is_guide_question("  ", RECS)


def test_prompt_has_no_example_sentences():
    """condition_extractor 머리말: 프롬프트에 예시 문장을 넣으면 그대로 따라 한다."""
    assert "예:" not in guide_router.PROMPT and "예시" not in guide_router.PROMPT


# --- 실제 LLM (키가 있을 때만) ------------------------------------------------------------
# 판정 기준 질문: eval/guide_questions.jsonl의 이어진 질문 중 근거가 있는 것(사람이 판정 문서 질문으로 고른 것).
# 화면에는 그 질문의 제도(service_ids)가 나와 있다고 본다 (다른 제도를 물으면 search가 맞다)
NAMES = {"336000000096": "주거급여 (맞춤형 급여)", "WII000001410": "생계급여", "149200005007": "국민취업지원제도"}
GUIDE_QS = [(q["question"], [Recommendation(service_name=NAMES[s], agency="", reason="", benefit="", how_to_apply="",
                                            service_id=s) for s in q["service_ids"]])
            for q in map(json.loads, Path(config.EVAL_DIR / "guide_questions.jsonl").read_text(encoding="utf-8").splitlines())
            if q["mode"] == "followup" and not q.get("expect_no_guide")]
SEARCH_QS = ["소득은 월 150만 원이에요", "저는 서울 관악구에 살아요", "혼자 살고 있고 28살이에요",
             "이것 말고 교통비 지원도 있나요?", "신청은 어디서 하나요?", "언제까지 신청해야 하나요?",
             "한 달에 얼마 받을 수 있어요?", "취업 준비 중인데 다른 지원도 알려 주세요"]


@pytest.mark.skipif(not config.OPENAI_API_KEY, reason="OPENAI_API_KEY 없음")
def test_real_llm_routes_labeled_questions():
    wrong_guide = [q for q, recs in GUIDE_QS if not guide_router.is_guide_question(q, recs)]
    wrong_search = [q for q in SEARCH_QS if guide_router.is_guide_question(q, RECS)]
    assert len(wrong_guide) <= 1, wrong_guide        # 판정 질문을 놓쳐도 버튼으로 물을 수 있어 1개까지 허용
    assert wrong_search == [], wrong_search          # 정보 덧붙이기를 판정 문서로 보내면 다시 추천을 못 받는다
