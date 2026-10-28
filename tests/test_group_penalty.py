"""질문에 없는 특정 대상 전용 제도를 검색 순위에서 뒤로 (src/retrieve/group_penalty.py, WORK_ITEMS R1 2단계). 색인 없이."""
from src.common.schema import Chunk
from src.retrieve import group_penalty, hybrid


def chunk(sid, name):
    return Chunk(chunk_id=f"{sid}#지원내용", service_id=sid, service_name=name, section="지원내용", text=name)


RANKED = [(chunk("D1", "장애학생 취업지원"), 0.05), (chunk("N1", "국민취업지원제도"), 0.04),
          (chunk("D2", "장애인 취업성공패키지"), 0.03), (chunk("N2", "청년도전지원사업"), 0.02)]


def ids(ranked):
    return [c.service_id for c, _ in ranked]


def test_group_only_programs_go_behind_when_question_lacks_the_group():
    out = group_penalty.penalize_other_group(RANKED, "대학을 졸업했는데 아직 취업을 못 했어요")
    assert ids(out) == ["N1", "N2", "D1", "D2"]                  # 빼지 않고 뒤로 (서로의 순서는 그대로)


def test_question_with_the_group_keeps_order():
    assert ids(group_penalty.penalize_other_group(RANKED, "장애가 있는 졸업생인데 취업 지원이 있나요")) == ["D1", "N1", "D2", "N2"]


def test_zero_penalty_turns_it_off():
    assert group_penalty.penalize_other_group(RANKED, "졸업했는데 취업을 못 했어요", 0) == RANKED


def test_only_service_mode_uses_it(monkeypatch):
    """hybrid_filter(서비스 기본값)에서만 감점한다. vector·bm25·hybrid 실험 모드는 그대로."""
    seen = []
    monkeypatch.setattr(hybrid.vector_store, "vector_search", lambda q, n, where: [(c, s) for c, s in RANKED])
    monkeypatch.setattr(hybrid.vector_store, "bm25_search", lambda q, n, where: [(c, s) for c, s in RANKED])
    monkeypatch.setattr(hybrid.vector_store, "build_where", lambda filters: None)
    real = group_penalty.penalize_other_group
    monkeypatch.setattr(group_penalty, "penalize_other_group", lambda r, q, p=0: seen.append(p) or real(r, q, p))
    assert [c.service_id for c in hybrid.search("졸업했는데 취업을 못 했어요", {"region": "서울"}, k=2)] == ["N1", "N2"]
    assert [c.service_id for c in hybrid.search("졸업했는데 취업을 못 했어요", {}, k=2, mode="hybrid")] == ["D1", "N1"]
    assert seen[0] == group_penalty.GROUP_PENALTY and seen[-1] == 0
