"""검색 지표 (기획서 9.2). 잠정 목표: Hit Rate@5 >= 0.80, Recall@5 >= 0.70, MRR >= 0.60.

retrieved: 질문별 검색된 service_id 순위 리스트, relevant: 질문별 정답 service_id 집합.
채점 단위는 service_id (조각이 아니라 서비스 기준).
"""
from __future__ import annotations

TARGETS = {"hit_rate@5": 0.80, "recall@5": 0.70, "mrr": 0.60}


def hit_rate_at_k(retrieved: list[list[str]], relevant: list[set[str]], k: int) -> float:
    raise NotImplementedError


def recall_at_k(retrieved: list[list[str]], relevant: list[set[str]], k: int) -> float:
    raise NotImplementedError


def mrr(retrieved: list[list[str]], relevant: list[set[str]]) -> float:
    raise NotImplementedError
