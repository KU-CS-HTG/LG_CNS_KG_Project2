"""이어진 질문 분류: '~도 지원받을 수 있나요?'는 안내받은 제도의 지원 범위(guide), 다른 제도는 분명히 말할 때만 search.
LLM은 부르지 않는다 (프롬프트에 규칙이 있는지만 본다. 실제 분류는 tests/test_guide_router.py의 키가 있을 때 테스트)."""
from src.generate import guide_router


def test_prompt_sends_also_covered_questions_to_guide():
    guide_part, search_part = guide_router.PROMPT.split("- search:")
    assert "~도 지원받을 수 있나요?" in guide_part and "지원 범위" in guide_part


def test_prompt_needs_explicit_words_for_other_programs():
    search_part = guide_router.PROMPT.split("- search:")[1]
    for words in ("'다른 제도'", "'비슷한 제도'", "'추천'", "지원하는 제도가 있나요"):
        assert words in search_part
