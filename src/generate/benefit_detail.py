"""W2: deterministic 지원내용 detail; no lookup, LLM, calculation or paraphrase.

Return the complete support-content block when it contains explicit numeric KRW
amounts in a benefit context. Keeping the whole section preserves parent headings,
alternative types and shared footnotes/nonpayment conditions; it is intentionally
not a list of isolated monetary sentences. No length/line cap is applied.

Ambiguous evidence fails closed (empty detail, existing summary stays): mixed
service identities, conflicting duplicate IDs, unfamiliar IDs, mixed unsuffixed/
suffixed forms, or numbered fragments other than 1..N. Exact duplicate Chunks are
ignored. Numeric fragments are concatenated without adding boundary characters,
matching ingest.chunker's lossless split contract. A missing final fragment cannot
be detected without an expected count: normal callers must supply expanded service
evidence. Bare text is accepted; only an exact leading service-name line is removed.
"""
from __future__ import annotations

import re

from src.common.schema import Chunk

# Detection only: these matches never replace or extract text out of the source.
_MONEY = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:억|천만|백만|십만|만|천|백)?\s*원")
_BENEFIT = re.compile(
    r"지원(?!\s*(?:대상|내용|조건|요건|안내))|지급|수당|급여|인센티브|장려금|보조금|환급|보상|감면|면제|할인|대출|융자"
)
_THRESHOLD = re.compile(r"소득|재산|자산|매출|연봉|월급|보증금|예산|사업비")
_COMPARISON = re.compile(r"^\s*(?:[이가은는을를]\s*)?(?:이하|이상|미만|초과|미달)")
_CAP_PAYMENT = re.compile(r"^\s*(?:로|씩|까지)?\s*(?:지급|지원|감면|환급)")
_SENTENCE_END = re.compile(r"[!?;。]|\.(?!\d)")


def _has_amount(text: str) -> bool:
    """Explicit currency plus benefit context; dates/counts/% alone are insufficient.

    Whitespace is collapsed ONLY for classification: a newline does not sever an
    income condition. Each amount uses its own sentence context, bounded by the
    adjacent currency amounts. A nearer preceding threshold cue takes precedence;
    a subsequent payment cue can support an amount without a preceding payment
    noun, but cannot cross another amount/threshold. A comparison needs an explicit
    payment noun or an immediately following payment verb (a cap, e.g. 이하 지급).
    Generic support headings are not payment cues. This conservative heuristic is
    not an eligibility/general language parser; spelled Korean numerals, foreign
    currencies and implicit units are not interpreted. Displayed source is intact.
    """
    analysis = " ".join(text.split())
    for sentence in _SENTENCE_END.split(analysis):
        amounts = list(_MONEY.finditer(sentence))
        for i, match in enumerate(amounts):
            prefix = sentence[amounts[i - 1].end() if i else 0:match.start()]
            suffix = sentence[match.end():amounts[i + 1].start() if i + 1 < len(amounts) else len(sentence)]
            limits = list(_THRESHOLD.finditer(prefix))
            benefits = list(_BENEFIT.finditer(prefix))
            if limits and (not benefits or limits[-1].start() > benefits[-1].start()):
                continue
            if benefits:
                return True
            # "100만원 이하 ... 수당 지급" is not "20만원 이하 지급".
            comparison = _COMPARISON.match(suffix)
            if comparison and not _CAP_PAYMENT.match(suffix[comparison.end():]):
                continue
            payment = _BENEFIT.search(suffix)
            threshold = _THRESHOLD.search(suffix)
            if payment and (not threshold or payment.start() < threshold.start()):
                return True
    return False


def _body(chunk: Chunk) -> str:
    for newline in ("\r\n", "\n"):
        prefix = chunk.service_name + newline
        if chunk.text.startswith(prefix):
            return chunk.text[len(prefix):]
    return chunk.text


def extract(chunks: list[Chunk]) -> list[str]:
    """Faithful complete support-content detail for one service, or []."""
    if not chunks:
        return []
    identities = {(c.service_id, c.service_name) for c in chunks}
    if len(identities) != 1:
        return []
    service_id, name = next(iter(identities))
    if not service_id or not name:
        return []

    unique: dict[str, Chunk] = {}
    for c in chunks:
        if c.section != "지원내용":
            continue
        if c.chunk_id in unique and c != unique[c.chunk_id]:
            return []
        unique[c.chunk_id] = c
    if not unique:
        return []

    base = f"{service_id}#지원내용"
    if base in unique:
        if len(unique) != 1:
            return []
        text = _body(unique[base])
    else:
        numbered = {}
        for cid, c in unique.items():
            match = re.fullmatch(re.escape(base) + r"-([1-9]\d*)", cid)
            if not match:
                return []
            numbered[int(match[1])] = _body(c)
        if sorted(numbered) != list(range(1, len(numbered) + 1)):
            return []
        if any(not body for body in numbered.values()):
            return []
        text = "".join(numbered[n] for n in range(1, len(numbered) + 1))
    return [text] if text.strip() and _has_amount(text) else []
