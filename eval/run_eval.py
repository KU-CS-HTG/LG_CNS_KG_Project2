"""[C. 평가] 조건 필터 + 검색이 기대한 제도를 찾는지 확인 (LLM 호출 없음)

사용법:
    python eval/run_eval.py
"""
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from src.retriever import search  # noqa: E402

QUESTIONS = Path(__file__).parent / "questions.jsonl"


def main():
    cases = [json.loads(l) for l in QUESTIONS.read_text(encoding="utf-8").splitlines() if l.strip()]
    passed = 0
    for c in cases:
        docs = search(c["question"], c["user"], k=3)
        got = [d.metadata["service_id"] for d in docs]
        # expected: top-3 안에 있어야 함 / forbidden: 조건 필터로 걸러져야 함
        ok = (all(e in got for e in c["expected_ids"])
              and not any(f in got for f in c.get("forbidden_ids", [])))
        passed += ok
        print(f"{'✅' if ok else '❌'} {c['user']} | {c['question']} → {got}")
    print(f"\n통과 {passed}/{len(cases)}")


if __name__ == "__main__":
    main()
