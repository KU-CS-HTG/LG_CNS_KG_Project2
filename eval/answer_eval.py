"""답변 지표 (기획서 9.3, C 담당): Answer Correctness, Faithfulness, Citation Accuracy.

근거 없음 질문(expect_no_answer)은 '모른다'고 답하고 추측하지 않았는지를 Correctness로 본다.
"""
from __future__ import annotations

from src.common.schema import Answer


def answer_correctness(answer: Answer, question: dict) -> float:
    raise NotImplementedError


def faithfulness(answer: Answer, evidence_text: str) -> float:
    raise NotImplementedError


def citation_accuracy(answer: Answer, evidence_text: str) -> float:
    raise NotImplementedError
