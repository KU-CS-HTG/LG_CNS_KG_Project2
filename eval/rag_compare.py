"""'왜 RAG인가' 비교 (C 담당, 발표용): 같은 질문을 검색 없는 gpt-4o-mini와 우리 서비스에 넣고 같은 기준으로 채점한다.

- Answer Correctness: answer_eval의 같은 채점 프롬프트(LLM)로 두 답변 모두 채점한다. 근거 없음 질문도 둘 다 LLM 판정
  (answer_eval은 우리 답변의 근거 없음 질문을 코드로 판정하지만, 여기서는 두 쪽을 같은 방법으로 보기 위해 바꿨다).
- 정답 제도 안내: 질문 세트의 relevant_service_names가 답변 글에 있는가 (코드). 두 쪽 모두 같은 이름 대조라
  우리 쪽도 answer_eval의 service_id 대조보다 낮게 나올 수 있다. 괄호 안 부제는 떼고도 본다.
- 출처 링크: 답변에 링크(http)가 있는 비율. 링크가 실제 공식 페이지인지는 보지 않는다 (사람이 확인할 몫).
- 일반 LLM 프롬프트는 공정하게: 제도·자격·신청 방법 안내를 요청하고, 모르면 모른다고 하라고 적는다 (같은 모델·temperature 0).
우리 답변은 answer.text(답변 전문: 카드의 이유·지원내용·신청방법·확인 필요·근거·링크 포함)로 채점한다.

실행:
    python -m eval.rag_compare --limit 3 --no-save   # 도구 점검 (질문 3개)
    python -m eval.rag_compare                       # 전체 (질문 하나에 LLM 약 7번), 결과를 eval/results/에 저장
    python -m eval.rag_compare --demo                # 시연 후보(eval/demo_questions.jsonl): 채점 없이 두 쪽 답을 나란히 .md로
최종 발표 숫자는 코드 동결(10/29) 뒤 최종 모델로 한 번 돌린다.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from eval import answer_eval
from src.common import config
from src.common.schema import Answer
from src.generate import condition_extractor, generator

PLAIN_PROMPT = """너는 한국 정부 지원제도 안내자다. 사용자의 상황에 맞는 정부·지자체 지원제도를 찾아
제도명, 지원 대상·자격, 지원 내용, 신청 방법을 한국어로 안내한다. 확실하지 않은 내용은 확실하지 않다고 밝히고,
해당하는 제도를 모르면 모른다고 답한다."""

DEMO_QUESTIONS_PATH = Path(__file__).with_name("demo_questions.jsonl")   # 시연 후보 (문항마다 보여 줄 것: shows)
_URL_RE = re.compile(r"https?://\S+")
_client = None


def plain_answer(question: str) -> str:
    """검색 없이 gpt-4o-mini에 질문만 넣은 답 (우리 서비스와 같은 모델·temperature)."""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=60)
    resp = _client.chat.completions.create(
        model=config.GENERATION_MODEL, temperature=0,
        messages=[{"role": "system", "content": PLAIN_PROMPT}, {"role": "user", "content": question}])
    return resp.choices[0].message.content or ""


def service_text(answer: Answer) -> str:
    """채점할 우리 답변 글 = answer.text (answer_eval과 같다). answer.text는 답변 전문이라 카드의 이유·지원내용·
    신청방법·확인 필요·근거·상세 링크가 이미 들어 있다 (카드 내용을 다시 붙였더니 같은 내용이 두 번씩 들어갔다)."""
    return answer.text


NO_ANSWER_PROMPT = """너는 답변 채점자다. [질문]은 공식 자료(보조금24)에 근거가 없는 질문이다
(제도가 없거나, 종료됐거나, 수집 범위 밖). [답변]이 어느 쪽인지 판정해 JSON 객체 하나를 출력한다.

- "보류": 자료·근거를 찾지 못했거나 확실하지 않다고 밝히고, 구체적인 자격·금액·기간을 사실처럼 단정하지 않았다.
- "단정": 특정 제도의 자격·금액·기간·신청 방법을 사실처럼 구체적으로 안내했다 (끝에 '확인하세요'를 붙였어도 단정이다).

출력 형식: {"verdict": "보류" 또는 "단정", "reason": 한 문장}"""


def correctness(text: str, question: dict) -> float:
    """answer_eval과 같은 채점 프롬프트로 글 답변을 채점 (충족 1 / 부분 충족 0.5 / 불충족 0).

    근거 없음 질문은 기대 답변과 비교하지 않고 '보류했는가'만 본다 (보류 1 / 단정 0, 두 쪽 같은 프롬프트).
    기대 답변('가입 조건을 근거와 함께 안내한다')과 비교하면, 일반 LLM이 지어낸 조건(q25 청년도약계좌
    '연소득 3,000만 원 이하')으로 충족을 받고 근거 없다고 답한 우리 서비스가 불충족을 받았다.
    answer_eval이 우리 답변의 근거 없음 질문을 코드로 판정하는 것과 같은 기준이다."""
    if question.get("expect_no_answer"):
        user = f"[질문]\n{question.get('question', '')}\n\n[답변]\n{text}"
        out = condition_extractor._chat_json(NO_ANSWER_PROMPT, user)
        return 1.0 if str(out.get("verdict", "")).strip() == "보류" else 0.0
    user = (f"[질문]\n{question.get('question', '')}\n\n[기대하는 답변]\n{question.get('expected_answer', '')}"
            f"\n\n[답변]\n{text}")
    out = condition_extractor._chat_json(answer_eval.CORRECTNESS_PROMPT, user)
    return answer_eval._CORRECTNESS_SCORE.get(str(out.get("verdict", "")).strip(), 0.0)


def name_match(text: str, question: dict) -> dict:
    """정답 제도 이름이 답변 글에 있는가. 정답이 없거나 근거 없음 질문이면 빈 dict (평균에서 빠진다)."""
    names = [n for n in question.get("relevant_service_names") or [] if n]
    if not names or question.get("expect_no_answer"):
        return {}
    body = answer_eval._squash(text)
    hit = [n for n in names if any(v and v in body for v in _name_variants(n))]
    return {"service_hit": 1.0 if hit else 0.0, "service_recall": len(hit) / len(names), "matched": hit}


def _name_variants(name: str) -> list[str]:
    """'청년월세 한시 특별지원(2차)' -> 공백 없앤 전체, 괄호 뗀 이름."""
    return list(dict.fromkeys([answer_eval._squash(name), answer_eval._squash(re.sub(r"\([^)]*\)", "", name))]))


def _score(text: str, q: dict, grade: bool = True) -> dict:
    graded = {"correctness": correctness(text, q)} if grade else {}
    return {**graded, "has_link": 1.0 if _URL_RE.search(text) else 0.0, **name_match(text, q), "chars": len(text)}


def compare(questions: list[dict], grade: bool = True) -> dict:
    """질문마다 두 쪽 답을 만들고 같은 기준으로 채점한다. 한 질문이 실패해도 나머지는 계속한다.
    grade=False(시연 후보 보기)면 LLM 채점 없이 답과 코드 지표만."""
    rows = []
    for q in questions:
        row = {"id": q.get("id"), "question": q["question"], "expect_no_answer": bool(q.get("expect_no_answer"))}
        try:
            plain = plain_answer(q["question"])
            ours, _ = generator._answer_with_evidence(q["question"])
            ours_text = service_text(ours)
            row.update(plain=_score(plain, q, grade), ours=_score(ours_text, q, grade),
                       plain_text=plain, ours_text=ours_text)
        except Exception as e:  # 네트워크·키 오류 등: 기록만 하고 평균에서 뺀다
            row["error"] = f"{type(e).__name__}: {e}"
        rows.append(row)

    ok = [r for r in rows if "error" not in r]

    def mean(side: str, key: str, rs=None):
        rs = [r for r in (ok if rs is None else rs) if key in r[side]]
        return round(sum(r[side][key] for r in rs) / len(rs), 4) if rs else None

    no_answer = [r for r in ok if r["expect_no_answer"]]
    metrics = {side: {"answer_correctness": mean(side, "correctness"),
                      "no_answer_correctness": mean(side, "correctness", no_answer),
                      "service_hit": mean(side, "service_hit"), "service_recall": mean(side, "service_recall"),
                      "link_rate": mean(side, "has_link")} for side in ("plain", "ours")}
    return {"experiment": "rag_compare", "time": datetime.now().isoformat(timespec="seconds"),
            "config": {"model": config.GENERATION_MODEL, "plain_prompt": PLAIN_PROMPT},
            "counts": {"total": len(rows), "scored": len(ok), "errors": len(rows) - len(ok)},
            "metrics": metrics, "questions": rows}


def format_report(result: dict) -> str:
    m, cnt = result["metrics"], result["counts"]
    fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
    lines = [f"=== 일반 LLM vs 우리 서비스 ({cnt['scored']}/{cnt['total']}문항, 오류 {cnt['errors']}, "
             f"모델 {result['config']['model']}) ===",
             f"{'지표':<22}{'일반 LLM':>10}{'우리 서비스':>12}"]
    for key, label in (("answer_correctness", "Answer Correctness"), ("no_answer_correctness", "근거 없음 질문 정확도"),
                       ("service_hit", "정답 제도 1개 이상 안내"), ("service_recall", "정답 제도 회수율"),
                       ("link_rate", "출처 링크 있음")):
        lines.append(f"{label:<22}{fmt(m['plain'][key]):>10}{fmt(m['ours'][key]):>12}")
    lines += ["", "차이가 큰 문항 (Correctness):"]
    diff = sorted((r for r in result["questions"] if "error" not in r and "correctness" in r["ours"]),
                  key=lambda r: -abs(r["ours"]["correctness"] - r["plain"]["correctness"]))
    for r in diff[:8]:
        lines.append(f"  {r['id']}  {r['question'][:40]}  ->  일반 {r['plain']['correctness']:.1f} / "
                     f"우리 {r['ours']['correctness']:.1f}")
    lines += [f"  {r['id']}  오류: {r['error']}" for r in result["questions"] if "error" in r]
    return "\n".join(lines)


def side_by_side(result: dict) -> str:
    """시연 후보 검토용 마크다운: 문항마다 무엇을 보여 줄지(shows)와 두 쪽 답을 나란히."""
    out = [f"# 시연 후보: 일반 LLM vs 우리 서비스 ({result['time']}, {result['config']['model']})", ""]
    for r in result["questions"]:
        out += [f"## {r['id']}  {r['question']}", ""]
        if r.get("shows"):
            out += [f"> 보여 줄 것: {r['shows']}", ""]
        if "error" in r:
            out += [f"오류: {r['error']}", ""]
            continue
        for side, label in (("plain", "일반 LLM (검색 없음)"), ("ours", "우리 서비스")):
            out += [f"### {label}", "", r[f"{side}_text"].strip(), ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    from eval.run_eval import QUESTIONS_PATH, RESULTS_DIR

    ap = argparse.ArgumentParser(description="일반 LLM(검색 없음) vs 우리 서비스 비교")
    ap.add_argument("--questions", default=None, help="질문 세트(jsonl). 기본: questions.jsonl (--demo면 demo_questions.jsonl)")
    ap.add_argument("--demo", action="store_true",
                    help="시연 후보: 채점 없이 두 쪽 답을 나란히 마크다운으로 저장 (eval/results/demo_*.md)")
    ap.add_argument("--limit", type=int, help="앞에서부터 N문항만")
    ap.add_argument("--only", help="이 문항만 (쉼표로 구분, 예: q01,q25)")
    ap.add_argument("--out-dir", default=str(RESULTS_DIR), help="결과 JSON 저장 폴더")
    ap.add_argument("--no-save", action="store_true", help="결과 파일을 저장하지 않는다")
    args = ap.parse_args(argv)

    if not config.OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY가 없습니다 (.env 확인).")
    path = Path(args.questions or (DEMO_QUESTIONS_PATH if args.demo else QUESTIONS_PATH))
    questions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only:
        wanted = set(args.only.split(","))
        questions = [q for q in questions if q.get("id") in wanted]
    result = compare(questions[: args.limit], grade=not args.demo)
    for r, q in zip(result["questions"], questions):
        r["shows"] = q.get("shows", "")
    if args.demo:
        md = side_by_side(result)
        if args.no_save:
            print(md)
            return
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        md_path = out / f"demo_{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        md_path.write_text(md, encoding="utf-8")
        print(f"시연 후보 {len(questions)}문항 -> {md_path}")
        return
    print(format_report(result))
    answer_eval._save(result, args, "rag_compare")


if __name__ == "__main__":
    main()
