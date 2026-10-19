"""답변 지표 (기획서 9.3, C 담당): Answer Correctness, Faithfulness, Citation Accuracy.

근거 없음 질문(expect_no_answer)은 '모른다'고 답하고 추측하지 않았는지를 Correctness로 본다.

- evidence_text: 답변 생성에 넘긴 근거 조각의 본문을 이어 붙인 문자열 (예: "\\n".join(c.text for c in chunks)).
- 점수는 모두 0~1. Citation Accuracy는 코드로, Faithfulness와 (근거 있는 질문의) Correctness는 LLM 판정(gpt-4o-mini)으로
  채점한다. 생성과 같은 모델이 채점하므로 점수가 후하게 나올 수 있다 (한계로 9.4에 적는다).

실행 (run_eval.py 주석: "답변 평가는 C가 eval/answer_eval.py와 연결한다"):
    python -m eval.answer_eval                       # eval/questions.jsonl 전체, 결과를 eval/results/에 저장
    python -m eval.answer_eval --limit 5 --no-save   # 앞 5문항만 빠르게
전제: 검색 색인이 있어야 한다 (python -m src.index.vector_store ...). 질문 하나에 LLM을 최대 4번 부른다
(조건 추출·생성·Faithfulness·Correctness). 답변 평가는 정답 서비스(relevant_service_ids)가 없어도 expected_answer로 채점한다.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from src.common import config
from src.common.schema import Answer
from src.generate import condition_extractor, generator
from src.generate.generator import GENERIC_REASON, NOT_IN_EVIDENCE

FAITHFULNESS_PROMPT = """너는 RAG 답변 채점자다. [근거]와 번호가 붙은 [주장]들이 주어진다.
각 주장이 [근거]만으로 뒷받침되는지 판정해 JSON 객체 하나를 출력한다.

규칙:
- 주장의 내용(대상·금액·기간·기관·절차)이 [근거]에 적혀 있거나 [근거]에서 바로 알 수 있으면 supported=true.
- [근거]에 없는 내용이 하나라도 들어 있거나 [근거]와 다르면 supported=false.
- "말씀하신 ..."처럼 사용자의 말을 옮긴 부분은 판정하지 않고, 제도에 대한 내용만 판정한다.
- 일반 상식으로 맞는 내용이라도 [근거]에 없으면 supported=false.

출력 형식: {"verdicts": [{"id": 주장 번호, "supported": true 또는 false}]}"""

CORRECTNESS_PROMPT = """너는 RAG 답변 채점자다. 사용자 [질문], 평가자가 정한 [기대하는 답변], 시스템의 [답변]이 주어진다.
[답변]이 [기대하는 답변]이 요구하는 내용과 행동을 충족하는지 판정해 JSON 객체 하나를 출력한다.

규칙:
- [기대하는 답변]에 적힌 요구 사항만 기준으로 본다. 거기에 없는 것을 요구하며 감점하지 않는다.
- "충족": 요구 사항을 모두 만족한다.
- "부분 충족": 요구 사항 중 일부만 만족한다.
- "불충족": 질문과 무관하거나, 요구 사항을 하나도 만족하지 못하거나, 근거 없이 자격을 단정했다.
- 자료에 없는 정보를 "확인 필요"나 "자료에 없음"으로 밝힌 것은 결함이 아니다.
- 문장 표현이 달라도 요구 사항을 만족하면 충족이다.

출력 형식: {"verdict": "충족" 또는 "부분 충족" 또는 "불충족", "reason": 한 문장}"""

_CORRECTNESS_SCORE = {"충족": 1.0, "부분 충족": 0.5, "불충족": 0.0}


def answer_correctness(answer: Answer, question: dict) -> float:
    """근거 없음 질문: 모른다고 답했으면 1, 제도를 안내했으면 0 (코드 판정).
    그 외: 근거 없음으로 답했으면 0, 아니면 expected_answer 기준 LLM 판정 (충족 1 / 부분 충족 0.5 / 불충족 0)."""
    if question.get("expect_no_answer"):
        return 1.0 if answer.no_evidence and not answer.recommendations else 0.0
    if answer.no_evidence or not answer.recommendations:
        return 0.0
    user = (f"[질문]\n{question.get('question', '')}\n\n[기대하는 답변]\n{question.get('expected_answer', '')}"
            f"\n\n[답변]\n{answer.text}")
    out = condition_extractor._chat_json(CORRECTNESS_PROMPT, user)
    return _CORRECTNESS_SCORE.get(str(out.get("verdict", "")).strip(), 0.0)


def faithfulness(answer: Answer, evidence_text: str) -> float:
    """답변의 주장(제도별 해당 가능 이유·지원내용·신청방법) 중 근거로 뒷받침되는 비율.
    주장이 없으면(근거 없음 응답) 지어낸 것이 없으므로 1."""
    claims = _claims(answer)
    if not claims:
        return 1.0
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(claims, 1))
    out = condition_extractor._chat_json(FAITHFULNESS_PROMPT, f"[근거]\n{evidence_text}\n\n[주장]\n{numbered}")
    supported = {v.get("id") for v in out.get("verdicts", []) if isinstance(v, dict) and v.get("supported") is True}
    return sum(1 for i in range(1, len(claims) + 1) if i in supported) / len(claims)


def citation_accuracy(answer: Answer, evidence_text: str) -> float:
    """인용 중 올바른 것의 비율 (코드 판정). 올바른 인용 = 근거 문구가 evidence_text에 실제로 있고,
    서비스명·소관기관·상세 링크가 모두 있으며, 그 인용이 붙은 제도의 것(서비스명 일치).
    제도를 안내했는데 인용이 하나도 없으면 0, 안내한 제도가 없으면 1."""
    pairs = [(rec, cit) for rec in answer.recommendations for cit in rec.citations]
    if not pairs:
        return 0.0 if answer.recommendations else 1.0
    body = _squash(evidence_text)
    ok = sum(1 for rec, cit in pairs
             if cit.quote and _squash(cit.quote) in body
             and cit.service_name and cit.agency and cit.detail_url
             and cit.service_name == rec.service_name)
    return ok / len(pairs)


def _claims(answer: Answer) -> list[str]:
    """채점할 주장 목록. 코드가 붙인 고정 문구(신청기한·'자료에 없음'·관련 제도 안내)는 주장이 아니므로 뺀다."""
    claims = []
    for r in answer.recommendations:
        how = r.how_to_apply.split(" / 신청기한: ")[0]
        for label, text in (("해당 가능 이유", r.reason), ("지원내용", r.benefit), ("신청방법", how)):
            if text and NOT_IN_EVIDENCE not in text and text != GENERIC_REASON:
                claims.append(f"[{r.service_name}] {label}: {text}")
    return claims


def _squash(s: str) -> str:
    return "".join((s or "").split())


# --- 실행 (질문 세트 전체 채점) ---------------------------------------------------
def answer_with_evidence(question: str) -> tuple[Answer, str]:
    """generator.answer_question과 같은 흐름으로 답하고, 생성에 넘긴 근거(evidence_text)도 함께 돌려준다.
    (answer_question은 Answer만 돌려주므로 Faithfulness·Citation Accuracy 채점용으로 근거까지 받는다.)"""
    answer, selected = generator._answer_with_evidence(question)
    return answer, "\n".join(c.text for cs in selected.values() for c in cs)


def evaluate(questions: list[dict]) -> dict:
    """질문마다 답변을 만들어 세 지표를 채점하고 평균을 낸다. 한 질문이 실패해도 나머지는 계속한다."""
    rows = []
    for q in questions:
        row = {"id": q.get("id"), "question": q["question"], "expect_no_answer": bool(q.get("expect_no_answer"))}
        try:
            answer, ev_text = answer_with_evidence(q["question"])
            row.update(
                no_evidence=answer.no_evidence,
                services=[r.service_name for r in answer.recommendations],
                correctness=answer_correctness(answer, q),
                faithfulness=faithfulness(answer, ev_text),
                citation_accuracy=citation_accuracy(answer, ev_text),
            )
        except Exception as e:  # 네트워크·키 오류 등: 기록만 하고 평균에서 뺀다
            row["error"] = f"{type(e).__name__}: {e}"
        rows.append(row)

    ok = [r for r in rows if "error" not in r]
    mean = lambda key, rs=ok: round(sum(r[key] for r in rs) / len(rs), 4) if rs else None  # noqa: E731
    return {
        "experiment": "answer_eval",
        "time": datetime.now().isoformat(timespec="seconds"),
        "config": {"model": config.GENERATION_MODEL, "k": config.DEFAULT_TOP_K},
        "counts": {"total": len(rows), "scored": len(ok), "errors": len(rows) - len(ok)},
        "metrics": {
            "answer_correctness": mean("correctness"),
            "faithfulness": mean("faithfulness"),
            "citation_accuracy": mean("citation_accuracy"),
            "no_answer_correctness": mean("correctness", [r for r in ok if r["expect_no_answer"]]),
        },
        "questions": rows,
    }


def format_report(result: dict) -> str:
    m, cnt = result["metrics"], result["counts"]
    fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
    lines = [f"=== 답변 평가 ({cnt['scored']}/{cnt['total']}문항 채점, 오류 {cnt['errors']}) ===",
             f"Answer Correctness {fmt(m['answer_correctness'])}  |  Faithfulness {fmt(m['faithfulness'])}"
             f"  |  Citation Accuracy {fmt(m['citation_accuracy'])}  |  근거 없음 질문 정확도 {fmt(m['no_answer_correctness'])}",
             "", "낮은 점수 (Correctness < 1 또는 Faithfulness < 1):"]
    low = [r for r in result["questions"] if "error" in r or r["correctness"] < 1 or r["faithfulness"] < 1]
    for r in low[:10]:
        detail = r.get("error") or (f"C {r['correctness']:.1f} / F {r['faithfulness']:.2f} / 제도 {len(r['services'])}개"
                                    + (" (근거 없음)" if r["no_evidence"] else ""))
        lines.append(f"  {r['id']}  {r['question'][:40]}  ->  {detail}")
    if not low:
        lines.append("  없음")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    from eval.run_eval import QUESTIONS_PATH, RESULTS_DIR, load_questions   # 질문 세트 형식·저장 위치는 B와 같게

    ap = argparse.ArgumentParser(description="답변 평가 (Answer Correctness / Faithfulness / Citation Accuracy)")
    ap.add_argument("--questions", default=str(QUESTIONS_PATH), help="질문 세트(jsonl)")
    ap.add_argument("--limit", type=int, help="앞에서부터 N문항만")
    ap.add_argument("--out-dir", default=str(RESULTS_DIR), help="결과 JSON 저장 폴더")
    ap.add_argument("--no-save", action="store_true", help="결과 파일을 저장하지 않는다")
    args = ap.parse_args(argv)

    if not config.OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY가 없습니다 (.env 확인).")
    from src.index import vector_store
    if vector_store.count() == 0:
        raise SystemExit("색인이 비어 있습니다. 먼저: python -m src.index.vector_store --sample  (또는 A의 파이프라인 결과 색인)")

    questions = load_questions(args.questions)[: args.limit]
    result = evaluate(questions)
    print(format_report(result))
    if not args.no_save:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"answer_eval_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  저장: {path}")


if __name__ == "__main__":
    main()
