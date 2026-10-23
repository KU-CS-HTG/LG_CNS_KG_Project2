"""답변 지표 (기획서 9.3, C 담당): Answer Correctness, Faithfulness, Citation Accuracy.

근거 없음 질문(expect_no_answer)은 '모른다'고 답하고 추측하지 않았는지를 Correctness로 본다.

- evidence_text: 답변 생성에 넘긴 근거 조각의 본문을 이어 붙인 문자열 (예: "\\n".join(c.text for c in chunks)).
- 점수는 모두 0~1. Citation Accuracy는 코드로, Faithfulness와 (근거 있는 질문의) Correctness는 LLM 판정(gpt-4o-mini)으로
  채점한다. 생성과 같은 모델이 채점하므로 점수가 후하게 나올 수 있다 (한계로 9.4에 적는다).

실행 (run_eval.py 주석: "답변 평가는 C가 eval/answer_eval.py와 연결한다"):
    python -m eval.answer_eval                       # eval/questions.jsonl 전체, 결과를 eval/results/에 저장
    python -m eval.answer_eval --limit 5 --no-save   # 앞 5문항만 빠르게
    python -m eval.answer_eval --guides              # 판정 기준 문서 근거 평가 (eval/guide_questions.jsonl, LLM 없음)
전제: 검색 색인이 있어야 한다 (python -m src.index.vector_store ...). 질문 하나에 LLM을 최대 4번 부른다
(조건 추출·생성·Faithfulness·Correctness). 답변 평가는 정답 서비스(relevant_service_ids)가 없어도 expected_answer로 채점한다.
정답 서비스가 있으면 '정답 제도를 안내했는가'(service_hit·service_recall)도 코드로 잰다 (색인 밖 정답은 분모에서 뺌).
"""
from __future__ import annotations

import argparse
import json
import re
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
- "찾는 도움:"으로 시작하는 주장은, 따옴표 안이 사용자가 찾는 도움을 사용자 표현 그대로 옮긴 것이다. 그 표현이 [근거]에 그대로 없어도 되며, 제도의 지원내용이 그 도움을 직접 주면 supported=true, 관련이 없거나 간접적이면 false다.
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


def faithfulness(answer: Answer, evidence_text: str, by_service: dict[str, str] | None = None) -> float:
    """답변의 주장(제도별 해당 가능 이유·지원내용·신청방법) 중 근거로 뒷받침되는 비율.
    주장이 없으면(근거 없음 응답) 지어낸 것이 없으므로 1.

    by_service(서비스명 -> 그 제도의 근거)가 있으면 제도마다 따로, 그 제도의 근거만 보고 채점한다.
    제도 5개·주장 15개를 한 번에 채점하면 따로 채점할 때 통과하는 주장도 근거 없음으로 흔들렸고(q09·q35),
    다른 제도의 근거로 통과하는 일도 막을 수 있다. 서비스명이 없는 제도는 전체 근거로 채점한다."""
    groups: dict[str, list[str]] = {}
    for r in answer.recommendations:
        text = (by_service or {}).get(r.service_name, evidence_text)
        groups.setdefault(text, []).extend(_claims(Answer(text="", recommendations=[r])))
    total = sum(len(cs) for cs in groups.values())
    if not total:
        return 1.0
    return sum(_supported(text, claims) for text, claims in groups.items() if claims) / total


def _supported(evidence_text: str, claims: list[str]) -> int:
    """claims 중 evidence_text로 뒷받침되는 주장 수 (LLM 한 번 호출)."""
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(claims, 1))
    out = condition_extractor._chat_json(FAITHFULNESS_PROMPT, f"[근거]\n{evidence_text}\n\n[주장]\n{numbered}")
    supported = {v.get("id") for v in out.get("verdicts", []) if isinstance(v, dict) and v.get("supported") is True}
    return sum(1 for i in range(1, len(claims) + 1) if i in supported)


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
        for label, text in (("해당 가능 이유", _need_claim(r.reason)), ("지원내용", r.benefit), ("신청방법", how)):
            if text and NOT_IN_EVIDENCE not in text and text != GENERIC_REASON:
                claims.append(f"[{r.service_name}] {label}: {text}")
    return claims


_NEED_REASON_RE = re.compile(r"^말씀하신 (.+)에 도움이 될 수 있는 제도예요")


def _need_claim(reason: str) -> str:
    """'말씀하신 ‘X’에 도움이 될 수 있는 제도예요'(generator가 meets_need 판정으로 만드는 이유)를 채점용 주장
    '찾는 도움: X — 이 제도의 지원내용이 이 도움을 직접 준다.'로 바꾼다 (채점 규칙에 이 형식의 판정법이 있다).
    - 그대로 넘기면 채점 규칙('말씀하신 ...'은 판정하지 않음)에 걸려 모두 근거 없음이 됐다 (Faithfulness 0.95 -> 0.77).
    - 'X와 직접 관련된다'로 바꿨을 때는 X(사용자 표현, '이자 부담을 줄일 방법')가 근거에 그대로 없다고 관련 있는 제도도
      떨어졌다 (학자금 이자지원). 관련 있는/없는 짝 8개 x 2회 시험에서 10/16 -> 14/16."""
    m = _NEED_REASON_RE.match(reason or "")
    return f"찾는 도움: {m.group(1)} — 이 제도의 지원내용이 이 도움을 직접 준다." if m else reason


def _squash(s: str) -> str:
    return "".join((s or "").split())


# --- 실행 (질문 세트 전체 채점) ---------------------------------------------------
def answer_with_evidence(question: str) -> tuple[Answer, str]:
    """generator.answer_question과 같은 흐름으로 답하고, 생성에 넘긴 근거(evidence_text)도 함께 돌려준다.
    (answer_question은 Answer만 돌려주므로 Faithfulness·Citation Accuracy 채점용으로 근거까지 받는다.)"""
    answer, selected = generator._answer_with_evidence(question)
    return answer, _evidence_text(selected)


def _evidence_text(selected: dict) -> str:
    return "\n".join(c.text for cs in selected.values() for c in cs)


def _evidence_by_service(selected: dict) -> dict[str, str]:
    """서비스명 -> 그 제도의 근거 텍스트 (Faithfulness 제도별 채점용)."""
    return {cs[0].service_name: _evidence_text({sid: cs}) for sid, cs in selected.items() if cs}


def service_match(answer: Answer, selected: dict, question: dict, indexed) -> dict:
    """정답 제도(relevant_service_ids)를 답변에서 안내했는가 (코드 판정, LLM 채점보다 객관적).

    - indexed(service_id) -> bool: 색인에 있는 제도인가. 청년 범위 밖이라 색인에 없는 정답은 시스템이 찾을 수 없으므로
      분모에서 빼고 개수만 따로 남긴다 (라벨 1차본에서 정답 88개 중 22개가 색인 밖이었다).
    - Recommendation에는 service_id가 없어(공용 schema) 생성에 넘긴 근거의 서비스명으로 되찾는다.
    정답이 없거나 근거 없음 질문이면 빈 dict (평균에서 빠진다)."""
    relevant = list(dict.fromkeys(question.get("relevant_service_ids") or []))
    if not relevant or question.get("expect_no_answer"):
        return {}
    reachable = [s for s in relevant if indexed(s)]
    out = {"unreachable_ids": [s for s in relevant if s not in reachable]}
    if not reachable:
        return out
    name_to_id = {cs[0].service_name: sid for sid, cs in selected.items() if cs}
    shown = {name_to_id.get(r.service_name) for r in answer.recommendations}
    hit = [s for s in reachable if s in shown]
    out.update(service_hit=1.0 if hit else 0.0, service_recall=len(hit) / len(reachable))
    return out


def _indexed(service_id: str) -> bool:
    from src.index import vector_store
    return bool(vector_store.get_service_chunks(service_id))


def evaluate(questions: list[dict], indexed=_indexed) -> dict:
    """질문마다 답변을 만들어 지표를 채점하고 평균을 낸다. 한 질문이 실패해도 나머지는 계속한다."""
    rows = []
    for q in questions:
        row = {"id": q.get("id"), "question": q["question"], "expect_no_answer": bool(q.get("expect_no_answer"))}
        try:
            answer, selected = generator._answer_with_evidence(q["question"])
            ev_text = _evidence_text(selected)
            row.update(
                no_evidence=answer.no_evidence,
                services=[r.service_name for r in answer.recommendations],
                correctness=answer_correctness(answer, q),
                faithfulness=faithfulness(answer, ev_text, _evidence_by_service(selected)),
                citation_accuracy=citation_accuracy(answer, ev_text),
                **service_match(answer, selected, q, indexed),
            )
        except Exception as e:  # 네트워크·키 오류 등: 기록만 하고 평균에서 뺀다
            row["error"] = f"{type(e).__name__}: {e}"
        rows.append(row)

    ok = [r for r in rows if "error" not in r]
    mean = lambda key, rs=ok: round(sum(r[key] for r in rs) / len(rs), 4) if rs else None  # noqa: E731
    labeled = [r for r in ok if "service_hit" in r]
    return {
        "experiment": "answer_eval",
        "time": datetime.now().isoformat(timespec="seconds"),
        "config": {"model": config.GENERATION_MODEL, "k": config.DEFAULT_TOP_K},
        "counts": {"total": len(rows), "scored": len(ok), "errors": len(rows) - len(ok),
                   "service_labeled": len(labeled),
                   "unreachable_ids": sum(len(r.get("unreachable_ids", [])) for r in ok)},
        "metrics": {
            "answer_correctness": mean("correctness"),
            "faithfulness": mean("faithfulness"),
            "citation_accuracy": mean("citation_accuracy"),
            "no_answer_correctness": mean("correctness", [r for r in ok if r["expect_no_answer"]]),
            "service_hit": mean("service_hit", labeled),
            "service_recall": mean("service_recall", labeled),
        },
        "questions": rows,
    }


def format_report(result: dict) -> str:
    m, cnt = result["metrics"], result["counts"]
    fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
    lines = [f"=== 답변 평가 ({cnt['scored']}/{cnt['total']}문항 채점, 오류 {cnt['errors']}) ===",
             f"Answer Correctness {fmt(m['answer_correctness'])}  |  Faithfulness {fmt(m['faithfulness'])}"
             f"  |  Citation Accuracy {fmt(m['citation_accuracy'])}  |  근거 없음 질문 정확도 {fmt(m['no_answer_correctness'])}",
             f"정답 제도 안내 (정답 있는 {cnt.get('service_labeled', 0)}문항): 1개 이상 안내 {fmt(m.get('service_hit'))}"
             f"  |  정답 회수율 {fmt(m.get('service_recall'))}"
             f"  (색인 밖이라 뺀 정답 {cnt.get('unreachable_ids', 0)}개)",
             "", "낮은 점수 (Correctness < 1 또는 Faithfulness < 1):"]
    low = [r for r in result["questions"] if "error" in r or r["correctness"] < 1 or r["faithfulness"] < 1]
    for r in low[:10]:
        detail = r.get("error") or (f"C {r['correctness']:.1f} / F {r['faithfulness']:.2f} / 제도 {len(r['services'])}개"
                                    + (" (근거 없음)" if r["no_evidence"] else ""))
        lines.append(f"  {r['id']}  {r['question'][:40]}  ->  {detail}")
    if not low:
        lines.append("  없음")
    return "\n".join(lines)


# --- 판정 기준 문서 근거 평가 (eval/guide_questions.jsonl, README 1.2) ---------------------------
# LLM 없이 코드로 센다. 질문 형식: {id, mode("card"|"followup"), question, service_ids, item(card), region,
# expected:[{file, page, phrases, doc_id?}], expect_no_guide}. 정답 쪽은 사람이 PDF를 읽고 정한다 (reviewed).
GUIDE_QUESTIONS_PATH = Path(__file__).with_name("guide_questions.jsonl")
# 답 문장에서 자격을 단정하는 표현 (판정 문서 규칙: 근거가 있어도 단정하지 않는다)
_ASSERTIVE_RE = re.compile(r"해당됩니다|해당합니다|해당돼요|받을 수 있습니다|받을 수 있어요|대상입니다|자격이 있습니다")


def guide_score(q: dict, cits: list, hits: list, text: str = "", region: str | None = None) -> dict:
    """판정 근거 1문항 채점. cits: 화면에 보인 Citation(kind="guide"), hits: 검색이 돌려준 GuideChunk 전체.

    - 정답 일치: 정답(쪽, 정답 문구)과 같은 쪽이고 그 근거가 나온 조각 원문에 정답 문구가 모두 있다
      (doc_id가 정답에 있으면 문서도 같아야 한다. 문서 목록 guide_sources.json이 생기기 전에는 쪽·문구로만 본다)
    - quote_accuracy: 보인 문구가 검색된 조각 원문의 일부인 비율 (요약·바꿔 쓰기 없음)
    - phrase_in_quote: 정답 근거에서 보인 문구 자체에 정답 문구가 들어 있는가 (잘라 낸 위치 점검)"""
    from src.generate import guide_evidence

    def source(c):                          # 근거 문구가 나온 검색 조각
        return next((h for h in hits if h.page == c.page and _squash(c.quote) in _squash(h.text)), None)

    matched, in_quote = 0, 0
    for e in q.get("expected") or []:
        phrases = [_squash(p) for p in e.get("phrases") or []]
        for c in cits:
            h = source(c)
            if (h and c.page == e.get("page") and (not e.get("doc_id") or h.doc_id == e["doc_id"])
                    and all(p in _squash(h.text) for p in phrases)):
                matched += 1
                in_quote += all(p in _squash(c.quote) for p in phrases)
                break
    n_exp = len(q.get("expected") or [])
    row = {"shown": len(cits), "pages": [c.page for c in cits],
           "quote_accuracy": (sum(1 for c in cits if source(c)) / len(cits)) if cits else 1.0,
           "assertive": bool(_ASSERTIVE_RE.search(text or "")),
           "region_leak": sum(1 for c in cits if (h := source(c)) and not guide_evidence._region_ok(h.scope, region))}
    if q.get("expect_no_guide"):
        row["no_guide_correct"] = 1.0 if not cits else 0.0
    if n_exp:
        row.update(guide_hit=1.0 if matched else 0.0, guide_recall=matched / n_exp,
                   phrase_in_quote=(in_quote / matched) if matched else 0.0)
    return row


def guide_evaluate(questions: list[dict]) -> dict:
    """판정 근거 질문마다 카드 근거(find_guides) 또는 후속 질문 답(answer_guide_question)을 만들어 채점한다."""
    from src.common.schema import Recommendation
    from src.generate import guide_evidence

    rows = []
    for q in questions:
        row = {"id": q.get("id"), "mode": q.get("mode"), "question": q["question"]}
        hits: list = []
        original = guide_evidence._search
        def recording(*a, **k):                 # 검색 결과를 함께 받아 원문 일치를 채점한다
            out = original(*a, **k)
            hits.extend(out)
            return out
        guide_evidence._search = recording
        try:
            ids, region = q.get("service_ids") or [], q.get("region")
            if q.get("mode") == "card":
                cits = guide_evidence.find_guides(ids[0] if ids else "", "", [q.get("item") or q["question"]], region)
                text = ""
            else:
                recs = [Recommendation("", "", "", "", "", service_id=sid) for sid in ids]
                ans = guide_evidence.answer_guide_question(q["question"], recs, region)
                cits, text = ans.citations, ans.text
            row.update(guide_score(q, cits, hits, text, region))
        except Exception as e:  # 한 문항이 실패해도 나머지는 계속
            row["error"] = f"{type(e).__name__}: {e}"
        finally:
            guide_evidence._search = original
        rows.append(row)

    ok = [r for r in rows if "error" not in r]
    def mean(key):
        vals = [r[key] for r in ok if key in r]
        return round(sum(vals) / len(vals), 4) if vals else None
    return {
        "experiment": "guide_eval",
        "time": datetime.now().isoformat(timespec="seconds"),
        "config": {"k": guide_evidence.GUIDE_K, "min_keyword_hits": guide_evidence.MIN_KEYWORD_HITS,
                   "keywords": "kiwi" if guide_evidence._kiwi_nouns("가구") is not None else "regex"},
        "counts": {"total": len(rows), "scored": len(ok), "errors": len(rows) - len(ok)},
        "metrics": {"guide_hit": mean("guide_hit"), "guide_recall": mean("guide_recall"),
                    "phrase_in_quote": mean("phrase_in_quote"), "quote_accuracy": mean("quote_accuracy"),
                    "no_guide_correctness": mean("no_guide_correct"),
                    "assertive": sum(r["assertive"] for r in ok), "region_leak": sum(r["region_leak"] for r in ok)},
        "questions": rows,
    }


def format_guide_report(result: dict) -> str:
    m, cnt = result["metrics"], result["counts"]
    fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
    lines = [f"=== 판정 기준 문서 근거 평가 ({cnt['scored']}/{cnt['total']}문항, 오류 {cnt['errors']}, "
             f"핵심 낱말 {result['config']['keywords']}) ===",
             f"정답 근거 1개 이상 {fmt(m['guide_hit'])}  |  정답 근거 회수율 {fmt(m['guide_recall'])}"
             f"  |  보인 문구에 정답 문구 {fmt(m['phrase_in_quote'])}  |  원문 일치 {fmt(m['quote_accuracy'])}",
             f"근거 없음 질문 정확도 {fmt(m['no_guide_correctness'])}  |  단정 표현 {m['assertive']}건"
             f"  |  다른 지역 문서 {m['region_leak']}건", "", "놓친 문항:"]
    low = [r for r in result["questions"] if "error" in r or r.get("guide_hit", 1) < 1 or r.get("no_guide_correct", 1) < 1]
    for r in low:
        detail = r.get("error") or f"보인 쪽 {r['pages']}"
        lines.append(f"  {r['id']}  {r['question'][:36]}  ->  {detail}")
    if not low:
        lines.append("  없음")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    from eval.run_eval import QUESTIONS_PATH, RESULTS_DIR, load_questions   # 질문 세트 형식·저장 위치는 B와 같게

    ap = argparse.ArgumentParser(description="답변 평가 (Answer Correctness / Faithfulness / Citation Accuracy)")
    ap.add_argument("--questions", default=None, help="질문 세트(jsonl). 기본: questions.jsonl (--guides면 guide_questions.jsonl)")
    ap.add_argument("--limit", type=int, help="앞에서부터 N문항만")
    ap.add_argument("--out-dir", default=str(RESULTS_DIR), help="결과 JSON 저장 폴더")
    ap.add_argument("--no-save", action="store_true", help="결과 파일을 저장하지 않는다")
    ap.add_argument("--guides", action="store_true", help="판정 기준 문서 근거 평가 (LLM 없이, B의 guide_store 필요)")
    args = ap.parse_args(argv)

    if args.guides:
        from src.generate import guide_evidence
        if not guide_evidence.available():
            raise SystemExit("판정 기준 문서 검색(src.index.guide_store)이 없습니다. B의 색인이 준비된 뒤 실행하세요.")
        path = Path(args.questions or GUIDE_QUESTIONS_PATH)
        questions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        result = guide_evaluate(questions[: args.limit])
        print(format_guide_report(result))
        _save(result, args, "guide_eval")
        return

    if not config.OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY가 없습니다 (.env 확인).")
    from src.index import vector_store
    if vector_store.count() == 0:
        raise SystemExit("색인이 비어 있습니다. 먼저: python -m src.index.vector_store --sample  (또는 A의 파이프라인 결과 색인)")

    questions = load_questions(args.questions or QUESTIONS_PATH)[: args.limit]
    result = evaluate(questions)
    print(format_report(result))
    _save(result, args, "answer_eval")


def _save(result: dict, args, prefix: str) -> None:
    if args.no_save:
        return
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{prefix}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  저장: {path}")


if __name__ == "__main__":
    main()
