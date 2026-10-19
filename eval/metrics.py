"""검색 지표: Hit Rate@K, Recall@K, MRR. (답변 지표는 C가 run_eval에서 확장)"""
from __future__ import annotations


def hit_rate_at_k(retrieved: list[list[str]], relevant: list[set[str]], k: int) -> float:
    raise NotImplementedError


def recall_at_k(retrieved: list[list[str]], relevant: list[set[str]], k: int) -> float:
    raise NotImplementedError


def mrr(retrieved: list[list[str]], relevant: list[set[str]]) -> float:
    raise NotImplementedError
