"""질문 하나를 넣어 답변이 어떤 과정으로 만들어졌는지 터미널에서 본다 (제도를 고른 이유·뺀 이유).

사용:  python -m src.generate.explain "질문"
검색 색인(Chroma)과 OPENAI_API_KEY가 필요하다. 앱을 띄운 터미널에서 같은 추적을 보려면 RAG_TRACE=1로 앱을 실행한다."""
from __future__ import annotations

import sys

from src.generate import generator, trace


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('사용: python -m src.generate.explain "질문"', file=sys.stderr)
        return 2
    trace.set_enabled(True)
    answer, _ = generator._answer_with_evidence(" ".join(args))
    trace.title("최종 안내")
    if answer.no_evidence:
        trace.out("  (근거 없음) " + answer.text.splitlines()[0])
    for i, r in enumerate(answer.recommendations, start=1):
        trace.out(f"  {i}. {r.service_name} ({r.agency}) - 확인 필요 {len(r.needs_check)}건")
        trace.out(f"     이유: {r.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
