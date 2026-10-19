"""평가 실행. 결과는 eval/results/<실험명>.json에 저장하고 요약을 기획서 9.4에 옮겨 적는다.

실험 순서 (기획서 6.2, 9.4): Baseline(SQL+키워드) -> Vector -> BM25 -> Hybrid -> Hybrid+정형 필터 -> 최종.
Top-K는 3 / 5 / 10으로 비교한다.
실행 예: python -m eval.run_eval --experiment hybrid_filter --k 5
"""
from __future__ import annotations

import json

from src.common import config

QUESTIONS_PATH = config.EVAL_DIR / "questions.jsonl"
EXPERIMENTS = ("baseline", "vector", "bm25", "hybrid", "hybrid_filter")


def load_questions() -> list[dict]:
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
