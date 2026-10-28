"""근거 없는 질문: 가장 가까운 조각도 유사도 하한 미달이면 검색은 빈 결과, 답변은 다시 찾지 않고 근거 없음 (WORK_ITEMS R2).
색인·모델·LLM 없이 가짜로."""
import pytest

from src.common.schema import Chunk
from src.generate import condition_extractor, generator
from src.index import embedder
from src.retrieve import hybrid

CHUNK = Chunk(chunk_id="S1#지원내용", service_id="S1", service_name="소상공인 손실보상금 지원", section="지원내용", text="손실보상")


@pytest.fixture
def store(monkeypatch):
    state = {"score": 0.55}
    monkeypatch.setattr(hybrid.vector_store, "vector_search", lambda q, n, where: [(CHUNK, state["score"])])
    monkeypatch.setattr(hybrid.vector_store, "bm25_search", lambda q, n, where: [(CHUNK, 3.0)])
    monkeypatch.setattr(hybrid.vector_store, "build_where", lambda filters: None)
    return state


@pytest.fixture
def real_model(monkeypatch):
    """bge-m3를 불러온 상태처럼 (하한은 실제 모델 유사도로 정한 값이라 모델을 불러왔을 때만 켠다)."""
    monkeypatch.setattr(embedder, "_model", object())
    monkeypatch.setattr(embedder, "_model_name", "BAAI/bge-m3")


def test_below_floor_returns_empty_with_flag(store, real_model):
    result = hybrid.search_with_info("해외 주식 손실 보전", {"age": 27})
    assert result.chunks == [] and result.below_floor is True
    store["score"] = 0.62                                              # 하한 위: 그대로 찾는다
    result = hybrid.search_with_info("골프 레슨비 지원", {"age": 27})
    assert [c.service_id for c in result.chunks] == ["S1"] and result.below_floor is False


def test_floor_off_without_real_model_or_in_experiment_modes(store, monkeypatch):
    monkeypatch.setattr(embedder, "_model", None)                      # 가짜 임베딩(테스트): 유사도 크기가 달라 끈다
    assert hybrid.search("해외 주식 손실 보전", {})
    monkeypatch.setattr(embedder, "_model", object())
    monkeypatch.setattr(embedder, "_model_name", "intfloat/multilingual-e5-large")   # 하한을 정하지 않은 모델
    assert hybrid.search("해외 주식 손실 보전", {})
    monkeypatch.setattr(embedder, "_model_name", "BAAI/bge-m3")
    assert hybrid.search("해외 주식 손실 보전", {}, mode="hybrid")      # 실험 모드는 그대로


def test_answer_stops_without_more_searches_or_llm_judging(monkeypatch):
    searches, llm = [], []
    monkeypatch.setattr(condition_extractor, "extract_conditions", lambda q: {"help": ["해외 주식 손실 보전"]})
    monkeypatch.setattr(condition_extractor, "_chat_json", lambda *a: llm.append(a) or {})

    def search_with_info(query, filters, k=5, mode="hybrid_filter"):
        searches.append(query)
        return hybrid.SearchResult([], [], dict(filters), False, below_floor=True)
    monkeypatch.setattr(hybrid, "search_with_info", search_with_info)
    answer = generator.answer_question("청년이 해외 주식 투자에서 손실을 보면 정부가 보전해 주는 제도가 있나요?")
    assert answer.no_evidence and not answer.recommendations
    assert len(searches) == 1 and llm == []                            # 핵심어로 다시 찾지 않고, 판정·생성 LLM 없음
