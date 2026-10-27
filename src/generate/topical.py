"""질문이 찾는 주제어와 후보 제도 본문이 맞는지로 판정 후보의 순서를 정한다 (코드, LLM 없음).

판정(LLM)에 넘기는 후보는 앞 10개뿐인데, 검색 순위는 '정부나 지자체 지원제도가 있어?'·'수원에 살고 있어' 같은 말에 끌려
핵심 주제('대중교통비')와 맞는 전국 제도(K-패스)가 21개 중 11번째, 판정 후보 10개 밖으로 밀렸다 (벡터 116위·BM25 295위).
그래서 '찾는 도움' 구절에서 일반어를 뺀 핵심어를 뽑고, 본문에 그 핵심어가 있는 후보를 앞으로 옮긴다 (나머지 순서는 그대로).
거르지는 않는다: 맞는 제도인지는 LLM 판정이 정한다."""
from __future__ import annotations

import re

from src.common.schema import Chunk
from src.generate import condition_extractor, trace

MATCH_RATIO = 0.6      # 핵심어의 글자 2개 묶음 중 본문에 있는 비율이 이 이상이면 주제가 맞는 것으로 본다
MIN_TERM = 2           # 핵심어 최소 글자 수
_JOSA_RE = re.compile(r"(?:이나|으로|에서|에게|까지|부터|처럼|만큼|을|를|이|가|은|는|도|나|의|에|와|과|로)$")
_STOP = (*condition_extractor._GENERIC_HELP, "정부", "지자체", "지방자치단체", "국가", "제도", "사업", "프로그램", "도움", "혜택",
         "서비스", "부담", "비용", "아낄", "있는", "있을", "받을", "받는", "알려", "찾고", "싶어", "어떻게", "어떤", "뭐가", "무엇", "관련")


def core_terms(help_: list[str] | None) -> list[str]:
    """'대중교통비를 아낄 수 있는 정부나 지자체 지원제도' -> ['대중교통비']."""
    terms: list[str] = []
    for phrase in help_ or []:
        for word in re.findall(r"[0-9A-Za-z가-힣]+", phrase):
            word = _JOSA_RE.sub("", word)
            rest = word
            for g in sorted(_STOP, key=len, reverse=True):
                rest = rest.replace(g, "")
            if len(word) >= MIN_TERM and len(rest) >= MIN_TERM and word not in terms:
                terms.append(word)
    return terms


def phrase_queries(help_: list[str] | None, question: str = "", limit: int = 2) -> list[str]:
    """'찾는 도움' 구절마다 핵심어만 이은 검색어 (앞 limit개). 핵심어가 없거나 질문 전체와 같으면 만들지 않는다."""
    queries: list[str] = []
    for phrase in (help_ or [])[:limit]:
        q = " ".join(core_terms([phrase]))
        if q and q != question.strip() and q not in queries:
            queries.append(q)
    return queries


def without_in_use_clause(question: str, in_use: list[str] | None) -> str:
    """이용 중인 제도를 말한 절을 뺀 질문 ('K-Digital Training을 듣고 있는데, 이것 말고도 취업준비생이 … 알려줘' -> '이것 말고도 취업준비생이 … 알려줘').
    질문 전체로 검색하면 그 제도와 비슷한 제도(훈련)가 검색 결과를 채워 후보가 15개로 바닥났다. 모두 지워지거나 바뀐 것이 없으면 ''."""
    if not in_use:
        return ""
    clauses = re.split(r"(?<=[,，、])\s*", question)
    kept = [c for c in clauses if not any(n.lower() in c.lower() for n in in_use)]
    cleaned = " ".join(kept).strip(" ,，、")
    return cleaned if kept and len(kept) < len(clauses) and len(cleaned) >= 6 else ""


def _bigrams(text: str) -> set[str]:
    squashed = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    return {squashed[i:i + 2] for i in range(len(squashed) - 1)}


def match_score(text: str, terms: list[str]) -> float:
    """핵심어마다 (글자 2개 묶음 중 본문에 있는 비율) 중 가장 높은 값. 핵심어가 없으면 0."""
    body = _bigrams(text)
    best = 0.0
    for term in terms:
        grams = _bigrams(term)
        if grams:
            best = max(best, len(grams & body) / len(grams))
    return best


def prioritize(chunks: list[Chunk], help_: list[str] | None) -> list[Chunk]:
    """주제가 맞는 제도(서비스 단위)를 앞으로 옮긴다. 핵심어를 못 뽑았거나 맞는 제도가 없으면 그대로."""
    terms = core_terms(help_)
    if not terms or not chunks:
        return chunks
    best: dict[str, float] = {}
    for c in chunks:
        best[c.service_id] = max(best.get(c.service_id, 0.0), match_score(f"{c.service_name} {c.text}", terms))
    fit = {sid for sid, s in best.items() if s >= MATCH_RATIO}
    if not fit or len(fit) == len(best):
        return chunks
    if trace.enabled():
        trace.title(f"주제어 '{', '.join(terms)}'가 본문에 있는 제도를 판정 후보 앞으로")
        names = trace.names(chunks)
        for sid in trace.ids(chunks):
            if sid in fit:
                trace.out(f"  + {sid}  {trace.short(names[sid])}  (일치 {best[sid]:.2f})")
    return [c for c in chunks if c.service_id in fit] + [c for c in chunks if c.service_id not in fit]
