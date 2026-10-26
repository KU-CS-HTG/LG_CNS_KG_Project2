"""질문 하나를 넣어 답변이 어떤 과정으로 만들어졌는지 터미널에서 본다 (제도를 고른 이유·뺀 이유).

사용:
  python -m src.generate.explain "질문"                     # 단계별 선정 과정
  python -m src.generate.explain "질문" --find "K-패스"      # 이 이름이 든 제도가 검색에서 몇 위였는지(색인에 있는지, 필터에 걸렸는지)
  ... --find "K-패스" --find-query "대중교통비 환급"          # 검색어를 바꿨을 때 순위 (질문 문장이 순위를 끌어내리는지 실험)
검색 색인(Chroma)과 OPENAI_API_KEY가 필요하다. 앱을 띄운 터미널에서 같은 추적을 보려면 RAG_TRACE=1로 앱을 실행한다."""
from __future__ import annotations

import argparse
import re
import sys

from src.common import config
from src.generate import condition_extractor, generator, trace

FIND_DEPTH = 1000   # 조각 기준으로 몇 위까지 순위를 찾는지 (색인 전체를 보지는 않는다)


def _squash(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s).lower()     # 'K-패스'와 'K 패스'와 'k패스'를 같게


def _service_ranks(chunks) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for chunk in chunks:
        ranks.setdefault(chunk.service_id, len(ranks) + 1)
    return ranks


def _rank_text(ranks: dict[str, int], sid: str) -> str:
    return f"{ranks[sid]}위" if sid in ranks else f"{FIND_DEPTH}조각 안에 없음"


def find_report(question: str, name_part: str, filters: dict, query: str | None = None) -> list[str]:
    """이름에 name_part가 든 제도마다 검색 방식별 순위를 적은 줄들. 필터 없이/있게 따로 보여 필터 때문인지 순위 때문인지 가른다."""
    from src.index import vector_store
    from src.retrieve import hybrid

    stored = vector_store._get_all(["metadatas"])
    wanted = _squash(name_part)
    found = sorted({m.get("service_id"): m.get("service_name") for m in stored["metadatas"]
                    if wanted and wanted in _squash(m.get("service_name") or "")}.items())
    if not found:
        return [f"  색인에 '{name_part}'이(가) 들어간 제도가 없습니다 (수집되지 않았거나 범위에서 제외됨: python -m src.ingest.coverage 로 확인)"]
    lines: list[str] = [f"  검색어: {query or question}"]
    question = query or question
    where = vector_store.build_where(filters)
    ranks: dict[str, dict[str, dict[str, int]]] = {}
    for label, w in (("필터 없음", None), ("필터 적용", where)):
        vec = [c for c, _ in vector_store.vector_search(question, FIND_DEPTH, w)]
        kw = [c for c, _ in vector_store.bm25_search(question, FIND_DEPTH, w)]
        fused = [c for c, _ in hybrid.rrf([vec, kw], config.HYBRID_WEIGHTS)]
        ranks[label] = {"벡터": _service_ranks(vec), "BM25": _service_ranks(kw), "합산": _service_ranks(fused)}
    for sid, name in found:
        chunks = vector_store.get_service_chunks(sid)
        meta = chunks[0].metadata if chunks else {}
        lines.append(f"  {sid}  {name}  (지역 '{meta.get('region') or '-'}', 기한 '{meta.get('deadline') or '-'}', 조각 {len(chunks)}개)")
        for label, by_mode in ranks.items():
            lines.append(f"      {label}: " + ", ".join(f"{mode} {_rank_text(r, sid)}" for mode, r in by_mode.items()))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="답변 선정 과정을 터미널에 출력")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--find", metavar="이름", help="이 이름이 든 제도의 검색 순위를 보여 준다")
    ap.add_argument("--find-query", metavar="검색어", help="--find의 순위를 질문 대신 이 검색어로 본다 (검색어를 다듬으면 순위가 오르는지 실험)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.question:
        print('사용: python -m src.generate.explain "질문" [--find 제도이름]', file=sys.stderr)
        return 2
    question = " ".join(args.question)
    trace.set_enabled(True)
    answer, _ = generator._answer_with_evidence(question)
    trace.title("최종 안내")
    if answer.no_evidence:
        trace.out("  (근거 없음) " + answer.text.splitlines()[0])
    for i, r in enumerate(answer.recommendations, start=1):
        trace.out(f"  {i}. {r.service_name} ({r.agency}) - 확인 필요 {len(r.needs_check)}건")
        trace.out(f"     이유: {r.reason}")
    if args.find:
        filters = condition_extractor.to_filters(condition_extractor.extract_conditions(question))
        trace.title(f"'{args.find}' 검색 순위 (질문 필터: {filters})")
        for line in find_report(question, args.find, filters, args.find_query):
            trace.out(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
