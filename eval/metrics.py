"""검색 지표 (기획서 9.2). 잠정 목표: Hit Rate@5 >= 0.80, Recall@5 >= 0.70, MRR >= 0.60.

입력 형식
- retrieved: 질문별로 검색된 service_id의 순위 리스트 (1위가 맨 앞). 서비스당 조각 1개로 이미 줄어든 `hybrid.search` 결과의
  service_id를 그대로 넣으면 된다. 같은 id가 여러 번 있어도 처음 나온 자리만 센다.
- relevant: 질문별 정답 service_id 집합 (`eval/questions.jsonl`의 relevant_service_ids).
채점 단위는 조각이 아니라 service_id다 (분할 방식이 바뀌어도 질문 세트를 그대로 쓰기 위함).

정답이 없는 질문(`expect_no_answer`, relevant가 빈 집합)은 검색 지표를 정의할 수 없으므로 계산에서 제외한다.
이런 질문은 `no_evidence_rate`로 따로 본다. 평가할 질문이 하나도 없으면 ValueError.

지표
- Hit Rate@K : 정답이 하나라도 상위 K개 안에 있는 질문의 비율
- Recall@K   : 질문별 (상위 K개 안의 정답 수 / 전체 정답 수)의 평균
- MRR        : 질문별 1 / (첫 정답의 순위)의 평균. 검색 결과에 정답이 없으면 0
"""
from __future__ import annotations

from collections.abc import Sequence

TARGETS = {"hit_rate@5": 0.80, "recall@5": 0.70, "mrr": 0.60}
DEFAULT_KS = (3, 5, 10)


def _evaluable(retrieved: Sequence[Sequence[str]], relevant: Sequence[set[str]]) -> list[tuple[list[str], set[str]]]:
    if len(retrieved) != len(relevant):
        raise ValueError(f"질문 수가 다릅니다: retrieved {len(retrieved)}개, relevant {len(relevant)}개")
    pairs = [(list(r), set(rel)) for r, rel in zip(retrieved, relevant) if rel]   # 정답 없는 질문 제외
    if not pairs:
        raise ValueError("평가할 질문이 없습니다 (정답이 있는 질문이 하나도 없음)")
    return pairs


def _check_k(k: int) -> None:
    if k < 1:
        raise ValueError(f"k는 1 이상이어야 합니다: {k}")


def hit_rate_at_k(retrieved: Sequence[Sequence[str]], relevant: Sequence[set[str]], k: int) -> float:
    _check_k(k)
    pairs = _evaluable(retrieved, relevant)
    return sum(bool(set(r[:k]) & rel) for r, rel in pairs) / len(pairs)


def recall_at_k(retrieved: Sequence[Sequence[str]], relevant: Sequence[set[str]], k: int) -> float:
    _check_k(k)
    pairs = _evaluable(retrieved, relevant)
    return sum(len(set(r[:k]) & rel) / len(rel) for r, rel in pairs) / len(pairs)


def mrr(retrieved: Sequence[Sequence[str]], relevant: Sequence[set[str]]) -> float:
    pairs = _evaluable(retrieved, relevant)
    total = 0.0
    for r, rel in pairs:
        for rank, service_id in enumerate(r, start=1):
            if service_id in rel:
                total += 1.0 / rank
                break
    return total / len(pairs)


def no_evidence_rate(retrieved_for_no_answer: Sequence[Sequence[str]]) -> float:
    """정답이 없는 질문(근거 없음)에서 검색이 빈 결과를 돌려준 비율. 높을수록 좋다 (관련 없는 제도를 내보내지 않음).
    관련도 하한선을 정할 때 이 값을 본다. 질문이 없으면 ValueError."""
    if not retrieved_for_no_answer:
        raise ValueError("근거 없음 질문이 없습니다")
    return sum(len(r) == 0 for r in retrieved_for_no_answer) / len(retrieved_for_no_answer)


def evaluate(retrieved: Sequence[Sequence[str]], relevant: Sequence[set[str]], ks: Sequence[int] = DEFAULT_KS) -> dict[str, float]:
    """한 번에 모든 지표를 계산한다. 키: hit_rate@K, recall@K, mrr, n_questions(계산에 쓴 질문 수), n_skipped(제외한 질문 수)."""
    n = len(_evaluable(retrieved, relevant))
    result: dict[str, float] = {}
    for k in ks:
        result[f"hit_rate@{k}"] = hit_rate_at_k(retrieved, relevant, k)
        result[f"recall@{k}"] = recall_at_k(retrieved, relevant, k)
    result["mrr"] = mrr(retrieved, relevant)
    result["n_questions"] = n
    result["n_skipped"] = len(relevant) - n
    return result


def check_targets(results: dict[str, float], targets: dict[str, float] = TARGETS) -> dict[str, bool]:
    """잠정 목표 달성 여부. results에 없는 지표(예: k=5를 계산하지 않음)는 결과에서 빠진다."""
    return {name: results[name] >= goal for name, goal in targets.items() if name in results}


if __name__ == "__main__":
    # 손으로 계산할 수 있는 예시로 구현을 확인한다: python -m eval.metrics
    retrieved = [["a", "b", "c"], ["x", "a"], ["m", "n", "o"]]
    relevant = [{"b"}, {"a", "z"}, {"q"}]
    expected = {
        "hit_rate@1": 0.0,        # 1위가 정답인 질문 없음
        "hit_rate@3": 2 / 3,      # 질문 1·2는 상위 3개 안에 정답, 3은 없음
        "recall@3": 1.5 / 3,      # (1/1 + 1/2 + 0) / 3
        "mrr": (1 / 2 + 1 / 2 + 0) / 3,   # 첫 정답이 2위, 2위, 없음
    }
    got = {
        "hit_rate@1": hit_rate_at_k(retrieved, relevant, 1),
        "hit_rate@3": hit_rate_at_k(retrieved, relevant, 3),
        "recall@3": recall_at_k(retrieved, relevant, 3),
        "mrr": mrr(retrieved, relevant),
    }
    ok = True
    for name, want in expected.items():
        match = abs(got[name] - want) < 1e-9
        ok &= match
        print(f"{'OK ' if match else 'FAIL'} {name:11s} 계산 {got[name]:.4f}  / 손계산 {want:.4f}")
    raise SystemExit(0 if ok else 1)
