"""지역 제도만 찾는 질문 ('대전에서만 받을 수 있는 제도')은 전국 제도를 섞지 않는다 (generator._asks_local_only)."""
import pytest

from src.generate import generator as g


@pytest.mark.parametrize("question", [
    "대전에 사는 청년인데 대전에서만 받을 수 있는 제도좀 추천해줘",
    "수원시에서만 하는 청년 지원 있어?",
    "부평구에서만 받을 수 있는 혜택 알려줘",
    "우리 동네에서만 받는 지원",
    "서울 사는데 지자체 제도만 알려줘",
    "부산시 자체 지원 사업이 궁금해요",
    "경기도 지역 혜택만 알려 주세요",
])
def test_local_only_questions(question):
    assert g._asks_local_only(question)


@pytest.mark.parametrize("question", [
    "대전에 사는 청년인데 받을 수 있는 지원이 있나요?",
    "온라인에서만 신청할 수 있나요?",
    "서울 사는 27살 취업준비생 지원 알려줘",
    "학교에서만 받을 수 있는 장학금이 있나요?",
])
def test_ordinary_region_questions_still_mix_national(question):
    assert not g._asks_local_only(question)
