"""같은 질문이면 같은 답: LLM 응답 저장소 (src/generate/llm_cache.py). 실제 API는 부르지 않는다."""
import json
import types

import pytest

from src.generate import condition_extractor as ce
from src.generate import llm_cache


class FakeClient:
    """부를 때마다 다른 응답을 돌려주는 가짜 OpenAI (실제 LLM의 흔들림 흉내)."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("network")
        content = json.dumps({"n": len(self.calls)})
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cache, "LOCAL_PATH", tmp_path / "llm_cache.db")
    monkeypatch.setattr(llm_cache, "SHARED_PATH", tmp_path / "llm_cache.jsonl")
    monkeypatch.setattr(llm_cache, "_shared", None)
    monkeypatch.delenv("LLM_CACHE", raising=False)
    fake = FakeClient()
    monkeypatch.setattr(ce, "_client", fake)
    return fake


def test_same_input_returns_same_answer_without_second_call(client):
    first = ce._chat_json("sys", "수원에 사는 27살")
    again = [ce._chat_json("sys", "수원에 사는 27살") for _ in range(3)]
    assert again == [first] * 3
    assert len(client.calls) == 1


def test_whitespace_differences_are_same_question(client):
    first = ce._chat_json("sys", "수원에 사는 27살")
    assert ce._chat_json("sys", "  수원에  사는\n27살 ") == first
    assert len(client.calls) == 1


def test_different_question_or_prompt_calls_again(client):
    ce._chat_json("sys", "수원에 사는 27살")
    ce._chat_json("sys", "부산에 사는 33살")
    ce._chat_json("other prompt", "수원에 사는 27살")
    assert len(client.calls) == 3


def test_seed_and_temperature_are_fixed(client):
    ce._chat_json("sys", "질문")
    assert client.calls[0]["seed"] == llm_cache.SEED
    assert client.calls[0]["temperature"] == 0


def test_cache_off_calls_every_time(client, monkeypatch):
    monkeypatch.setenv("LLM_CACHE", "0")
    a = ce._chat_json("sys", "질문")
    b = ce._chat_json("sys", "질문")
    assert a != b and len(client.calls) == 2
    assert not llm_cache.LOCAL_PATH.exists()


def test_failed_call_is_not_stored(client, monkeypatch):
    client.fail = True
    with pytest.raises(RuntimeError):
        ce._chat_json("sys", "질문")
    client.fail = False
    assert ce._chat_json("sys", "질문") == {"n": 2}


def test_caller_mutation_does_not_change_stored_answer(client):
    first = ce._chat_json("sys", "질문")
    first["help"] = ["월세"]                      # extract_conditions가 하는 것처럼 고쳐도
    assert ce._chat_json("sys", "질문") == {"n": 1}


def test_shared_file_gives_same_answer_on_another_pc(client, tmp_path, monkeypatch):
    """한 PC에서 만든 공유 파일만 있으면 다른 PC(빈 로컬 DB)도 LLM을 부르지 않고 같은 답을 낸다."""
    first = ce._chat_json("sys", "수원에 사는 27살")
    llm_cache.write_shared({llm_cache.key(ce.config.GENERATION_MODEL, "sys", "수원에 사는 27살")})

    monkeypatch.setattr(llm_cache, "LOCAL_PATH", tmp_path / "other_pc.db")
    monkeypatch.setattr(llm_cache, "_shared", None)
    other = FakeClient()
    monkeypatch.setattr(ce, "_client", other)
    assert ce._chat_json("sys", "수원에 사는 27살") == first
    assert other.calls == []


def test_shared_file_wins_over_local(client):
    ce._chat_json("sys", "질문")                                   # 로컬 {"n": 1}
    k = llm_cache.key(ce.config.GENERATION_MODEL, "sys", "질문")
    llm_cache.SHARED_PATH.write_text(json.dumps({"key": k, "model": "m", "data": {"n": "shared"}}) + "\n",
                                     encoding="utf-8")
    llm_cache._shared = None
    assert ce._chat_json("sys", "질문") == {"n": "shared"}


def test_threads_reading_shared_file_at_once_all_hit(client):
    """조건 추출·찾는 것 추출이 동시에 처음 공유 파일을 읽어도 둘 다 저장된 응답을 쓴다 (읽는 중인 목록을 보면 안 된다)."""
    import threading
    model = ce.config.GENERATION_MODEL
    lines = [json.dumps({"key": llm_cache.key(model, "sys", f"q{i}"), "model": model, "data": {"i": i}}) for i in range(20000)]
    llm_cache.SHARED_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    llm_cache._shared = None
    start = threading.Barrier(8)
    got = {}

    def ask(i):
        start.wait()
        got[i] = llm_cache.get(model, "sys", f"q{19999 - i}")

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == {i: {"i": 19999 - i} for i in range(8)}


def test_warm_writes_only_used_answers(client, tmp_path, monkeypatch):
    ce._chat_json("sys", "예전 질문")                               # warm과 관계없는 로컬 항목
    qfile = tmp_path / "q.jsonl"
    qfile.write_text(json.dumps({"question": "질문1"}, ensure_ascii=False) + "\n"
                     + json.dumps({"question": "질문1"}, ensure_ascii=False) + "\n", encoding="utf-8")
    from src.generate import generator
    monkeypatch.setattr(generator, "answer_question", lambda q: ce._chat_json("sys", q))
    assert llm_cache.warm([qfile]) == 1
    rows = [json.loads(l) for l in llm_cache.SHARED_PATH.read_text(encoding="utf-8").splitlines()]
    assert [r["key"] for r in rows] == [llm_cache.key(ce.config.GENERATION_MODEL, "sys", "질문1")]
    assert llm_cache._used is None
