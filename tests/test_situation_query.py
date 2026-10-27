"""찾는 도움을 말하지 않은 넓은 질문의 처지 핵심어 검색어 (generator._situation_query)."""
import pytest

from src.generate import generator as g


@pytest.mark.parametrize("question, expected", [
    ("귀농해서 농사 짓는 29살인데 받을 수 있는 지원이 있나요?", "귀농 농사 짓는"),
    ("취업 준비 중인 26살인데 받을 수 있는 지원이 있나요?", "취업 준비"),
    ("혼자 사는 70살 어르신인데 받을 수 있는 돌봄 서비스가 있나요?", "어르신 돌봄 서비스"),
])
def test_situation_words_without_age_region_filler(question, expected):
    assert g._situation_query(question, None) == expected


def test_region_words_are_left_to_filters():
    q = g._situation_query("대전에 사는 청년인데 대전에서만 받을 수 있는 제도좀 추천해줘", "대전")
    assert "대전" not in q and "추천" not in q and "청년" in q


def test_nothing_left_gives_empty():
    assert g._situation_query("받을 수 있는 지원이 있나요?", None) == ""
