"""판정 기준 문서 조항으로 이어진 질문에 자연스러운 답을 쓴다 (X1 후속 질문 답변).

guide_evidence.answer_guide_question이 찾고 코드로 검증한 조항(지역·관련도·원문 일부)만 LLM에 준다. LLM은 그
조항 안에서만 질문에 답하고, 문장마다 근거 조항 번호 [1]·[2]를 단다. 화면에는 답 아래에 같은 번호의 조항 원문
카드를 접어 두어 사용자가 출처를 직접 볼 수 있다 (app/main.py).

코드 검사 (하나라도 걸리면 None -> 고정 안내 문장 + 원문으로 되돌아간다):
- 조항으로 답할 수 없다고 했거나(answerable=false) 답이 비었다
- 근거 번호가 없거나 없는 번호를 달았다
- 자격을 단정했다 ('해당됩니다', '받을 수 있어요' 등, CLAUDE.md 판정 문서 규칙: "해당될 수 있음" + 최종 확인)
- 답에 단 번호마다 그 조항에서 글자 그대로 옮긴 근거 구절(evidence)이 없거나, 구절이 조항 원문에 없다
  (fit_check와 같은 방식. 실제 PDF에서 '동일가구로 편성되어 … 제외됨'을 '같은 가구로 볼 수 없다'로 뒤집어 썼다)
LLM 호출은 condition_extractor._chat_json (다른 LLM 호출과 같은 모델·설정).
프롬프트에 예시 문장을 넣지 않는다 (condition_extractor 머리말: 예시를 그대로 따라 함).
"""
from __future__ import annotations

import logging
import re

from src.generate import condition_extractor

log = logging.getLogger(__name__)

MAX_CLAUSE_CHARS = 1200       # LLM에 주는 조항 1개의 최대 길이 (화면 인용문보다 넓게 줘서 문맥을 읽게 한다)
MAX_ANSWER_CHARS = 900        # 이보다 길면 조항을 옮겨 적은 것으로 보고 쓰지 않는다 (원칙+예외+처지 합계)
MIN_EVIDENCE_CHARS = 8        # 근거 구절 최소 길이 (공백 뺀 글자 수, 낱말 하나로는 대조가 안 된다)

# eval/answer_eval._ASSERTIVE_RE와 같은 표현 + 자격 단정 말투. 아래 _SOFTEN으로 바꾼 뒤에도 남으면 답을 쓰지 않는다
_ASSERTIVE_RE = re.compile(r"해당됩니다|해당합니다|해당돼요|해당해요|받을 수 있습니다|받을 수 있어요|대상입니다|대상이에요|"
                           r"자격이 있습니다|자격이 있어요|자격이 됩니다|자격이 돼요|참여할 수 있(?:습니다|어요)")
# 기준을 설명하면서 쓴 단정 말투를 '~가능성이 있어요'·'해당될 수 있어요'로 낮춘다 (CLAUDE.md: "해당될 수 있음").
# gpt-4o-mini는 'Ⅱ유형은 취업경험이 없어도 지원받을 수 있어요'처럼 맞는 기준 설명에도 이 말투를 써서, 다시 쓰게 해도
# 두 번 다 같은 말투로 걸렸다. '대상입니다'·'자격이 있어요'는 바꿔 쓸 말이 마땅치 않아 그대로 막는다
_SOFTEN = (
    (re.compile(r"(받을|참여할) 수 있(?:습니다|어요)"), r"\1 가능성이 있어요"),
    (re.compile(r"해당(?:됩니다|합니다|돼요|해요)"), "해당될 수 있어요"),
)
_REF_RE = re.compile(r"\[(\d+)\]")
_REF_AFTER_STOP_RE = re.compile(r"([.!?])\s*((?:\[\d+\]\s*)+)")   # 마침표 뒤에 단 근거 번호
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")

PROMPT = """너는 정부 지원제도 상담에서, 공무원이 자격을 판정할 때 보는 판정 기준 문서의 조항을 읽고
사용자의 이어진 질문에 답하는 도구다. JSON 객체 하나만 출력한다. 키 순서대로 채운다:
{"evidence": [{"n": 조항 번호, "quote": "그 조항에서 글자 그대로 복사한 구절", "means": "이 구절이 질문에 대해 말하는 결론 한 줄"}],
 "answerable": true 또는 false,
 "rule": "원칙",
 "exception": "다만·예외·특례 (없으면 빈 문자열)",
 "situation": "사용자가 앞서 말한 처지에 맞춘 설명 (처지를 말하지 않았거나 조항으로 알 수 없으면 빈 문자열)"}

순서:
1. evidence: 질문에 답하는 구절을 조항에서 한 글자도 바꾸지 않고 복사한다 (한 문장 정도 길이, 원칙 구절과
   '다만'·예외 구절을 따로). means에는 그 구절이 말하는 그대로의 결론을 적는다. 구절에 '편성되어'·'포함'이 있으면
   포함된다는 뜻이고, '제외됨'·'불가'가 있으면 무엇이 제외되는지 구절에서 확인해 적는다.
2. answerable: evidence가 질문에 답하면 true, 아니면 false (rule·exception·situation은 빈 문자열).
3. rule: 질문에서 물은 대상(가구·소득·재산 등)이 원칙적으로 어떻게 되는지 1~2문장으로. 예외만 말하지 않는다.
   원칙이 적용되는 대상 조건(나이 범위·혼인 여부 등)을 빼지 않는다.
4. exception: 원칙과 다르게 보는 경우를 조건과 함께 1~2문장으로. 조항에 '다만'·'분리'·'예외'·'특례'·'인정하지
   않는'·'반영하지 않는' 구절이 질문 대상에 대해 있으면 반드시 쓴다.
5. situation: 앞서 말한 처지에 원칙과 예외 중 무엇을 확인해 봐야 하는지 1문장으로. 결론을 단정하지 않는다.
rule·exception·situation은 evidence의 means와 같은 결론만 쓴다. means와 반대되는 말을 쓰지 않는다.

규칙:
- 아래 [번호] 조항에 적힌 내용만 쓴다. 조항에 없는 기준·금액·예외·절차를 지어내거나 일반 상식으로 채우지 않는다.
- rule·exception·situation의 모든 문장 끝에 근거 조항 번호를 [1]처럼 단다. evidence에 넣은 번호만 쓴다.
- 조항이 여러 문서에서 왔으면 어느 문서(제도)의 기준인지 밝힌다. 안내받은 제도와 관계없는 문서는 쓰지 않는다.
- 조항이 안내받은 제도와 연계된 다른 사업(일경험 사업·인턴 사업 등)을 설명하면, 그 사업의 대상·기준을
  안내받은 제도의 대상·기준처럼 말하지 않는다. 질문에 답하는 조항이 그것뿐이면 answerable은 false다.
- 사용자가 앞서 말한 처지(나이·가구·소득 등)가 있으면 그 처지에 맞는 기준을 골라 설명한다.
- 조항의 조건을 바꾸거나 넓히지 않는다. 조건이 여러 개면 모두 말한다.
- 사용자의 자격을 단정하지 않는다. "해당됩니다", "받을 수 있어요", "대상입니다" 같은 말을 쓰지 않고
  "~로 볼 수 있어요", "~이면 포함될 수 있어요"처럼 조항의 기준을 설명한다.
- 조항 안에 '개정 예고'·'행정예고'라고 적힌 내용은 확정된 기준처럼 말하지 않고 개정 예고라고 밝힌다.
- 모든 문장은 친절한 존댓말(~해요, ~돼요)로 끝낸다. '~다'로 끝내지 않는다.
- 최종 자격 확인 안내 문장은 쓰지 않는다 (화면에 따로 나온다)."""
PARTS = ("rule", "exception", "situation")      # 답 = 원칙 -> 예외 -> 사용자 처지 순서로 코드가 이어 붙인다
NOT_ANSWERED = ""             # explain: 찾은 조항이 질문에 답하지 않는다 (None은 답 쓰기 실패: 원문은 보여 준다)


def explain(question: str, service_names: list[str], clauses: list[tuple[str, int | None, str]],
            context: str = "") -> str | None:
    """조항들로 질문에 답한 문장. clauses: [(문서 이름, 쪽, 조항 본문)], 번호는 1부터 이 순서.
    context: 사용자가 앞서 한 말 (처지에 맞는 기준을 고르게). LLM 실패·검사 탈락이면 None (고정 안내 문장).
    조항을 읽고 질문에 답하지 않는다고 했으면(answerable=false) NOT_ANSWERED: 앱은 '찾을 수 없어요'로 안내한다."""
    if not (question or "").strip() or not clauses:
        return None
    texts = [text for _, _, text in clauses]
    user = _user_message(question, service_names, clauses, context)
    try:
        data = condition_extractor._chat_json(PROMPT, user)
        if isinstance(data, dict) and data.get("answerable") is False:
            return NOT_ANSWERED
        answer, reason = _check(data, texts)
        if answer is None and reason:
            # 검사에 걸린 이유를 알려 주고 한 번만 다시 쓰게 한다 (단정 말투·근거 번호 빠짐은 내용이 맞아도 걸렸다)
            log.info("판정 기준 답변 다시 쓰기: %s", reason)
            answer, reason = _check(condition_extractor._chat_json(PROMPT, user + RETRY_NOTE.format(reason=reason)), texts)
    except Exception as e:  # 네트워크·키·JSON 오류: 고정 안내 문장 + 원문으로
        log.warning("판정 기준 문서 답변 생성 실패, 원문만 보여 줌: %s", e)
        return None
    if answer is None and reason:
        log.info("판정 기준 답변을 쓰지 않음: %s", reason)
    return answer


RETRY_NOTE = "\n\n앞서 쓴 답은 다음 이유로 쓸 수 없었다: {reason}\n규칙을 지켜 처음부터 다시 쓴다."


def checked(data: dict, clause_texts: list[str]) -> str | None:
    """LLM 응답을 코드로 검사한다. 통과하면 답 문장, 아니면 None."""
    return _check(data, clause_texts)[0]


def _check(data: dict, clause_texts: list[str]) -> tuple[str | None, str]:
    """(답 문장 또는 None, 탈락 이유). 이유가 ''면 다시 써도 소용없는 경우 (조항으로 답할 수 없음·형식 오류)."""
    if not isinstance(data, dict) or data.get("answerable") is not True:
        return None, ""
    grounded = _grounded_refs(data.get("evidence"), clause_texts)
    parts = {k: _sentences(data.get(k)) for k in PARTS}
    # 처지 문장은 '무엇을 확인해 보라'는 안내라 근거 번호가 없을 수 있다: 그 문장만 뺀다 (원칙·예외는 모두 번호 필수)
    parts["situation"] = [s for s in parts["situation"] if _REF_RE.search(s)]
    if len(grounded) == 1:             # 근거 구절이 조항 하나에서만 나왔으면 번호 없는 원칙·예외 문장은 그 조항이다
        [only] = grounded
        for k in ("rule", "exception"):
            parts[k] = [s if _REF_RE.search(s) else _cite(s, only) for s in parts[k]]
    unref = [s for k in ("rule", "exception") for s in parts[k] if not _REF_RE.search(s)]
    if unref:
        return None, "원칙·예외 문장마다 근거 조항 번호를 달아야 한다: " + " / ".join(unref)
    answer = " ".join(s for k in PARTS for s in parts[k])
    if not parts["rule"]:
        return None, "rule(원칙)이 비었다"
    if len(answer) > MAX_ANSWER_CHARS:
        return None, f"답이 너무 길다 ({MAX_ANSWER_CHARS}자 이내)"
    refs = {int(n) for n in _REF_RE.findall(answer)}
    if any(not 1 <= n <= len(clause_texts) for n in refs):
        return None, f"없는 조항 번호를 달았다: {sorted(refs)}"
    found = _ASSERTIVE_RE.search(answer)
    if found:
        return None, f"'{found.group()}'처럼 자격을 단정했다. 조항의 기준을 '~이면 지원 대상이 될 수 있어요'처럼 설명한다"
    missing = refs - grounded
    if missing:
        return None, f"조항 {sorted(missing)}의 구절을 evidence에 원문 그대로 옮기지 않았다"
    return answer, ""


def _cite(sentence: str, n: int) -> str:
    """문장 끝 마침표 앞에 근거 번호를 단다 ('원칙이에요.' -> '원칙이에요 [1].')."""
    body = sentence.rstrip()
    end = body[-1] if body[-1:] in ".!?" else ""
    return f"{body[:len(body) - len(end)].rstrip()} [{n}]{end}"


def _sentences(text) -> list[str]:
    """답 한 칸을 문장으로. 마침표 뒤에 단 근거 번호는 문장 안으로 ('않아요. [1]' -> '않아요 [1].'),
    기준 설명의 단정 말투는 낮춘다 (_SOFTEN)."""
    text = " ".join(str(text or "").split())
    for pattern, repl in _SOFTEN:
        text = pattern.sub(repl, text)
    text = _REF_AFTER_STOP_RE.sub(lambda m: f" {m.group(2).strip()}{m.group(1)} ", text).strip()
    return [s for s in _SENTENCE_END_RE.split(text) if s.strip()]


def _grounded_refs(evidence, clause_texts: list[str]) -> set[int]:
    """원문 그대로인 근거 구절이 실제로 들어 있는 조항 번호 (공백·줄바꿈은 무시하고 대조).
    LLM이 적은 번호(n)는 믿지 않고 구절이 든 조항을 코드가 찾는다: gpt-4o-mini가 구절은 정확히 옮기고 번호만
    조항 안의 문서 번호('4-83')와 헷갈려 4로 적은 일이 있었다. 지어낸 구절은 어느 조항에도 없어 그대로 막힌다."""
    out = set()
    bodies = [_squash(t) for t in clause_texts]
    for item in evidence if isinstance(evidence, list) else []:
        quote = _squash(str(item.get("quote") or "")) if isinstance(item, dict) else ""
        if len(quote) >= MIN_EVIDENCE_CHARS:
            out.update(i for i, body in enumerate(bodies, 1) if quote in body)
    return out


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _user_message(question: str, service_names: list[str], clauses: list[tuple[str, int | None, str]],
                  context: str = "") -> str:
    lines = []
    names = [n for n in service_names if n]
    if names:
        lines.append("안내받은 제도: " + ", ".join(names))
    if (context or "").strip():
        lines.append("사용자가 앞서 한 말: " + context.strip())
    lines.append("사용자 질문: " + question.strip())
    lines.append("")
    for i, (label, page, text) in enumerate(clauses, 1):
        where = f"{label}, {page}쪽" if page else label
        lines.append(f"[{i}] ({where})\n{text.strip()[:MAX_CLAUSE_CHARS]}")
    return "\n".join(lines)
