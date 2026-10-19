"""③ 임베딩 - 로컬 다국어 모델 (bge-m3 우선, 자원 부족 시 multilingual-e5). 후보는 소규모 비교 후 확정."""
from __future__ import annotations


def embed_texts(texts: list[str]) -> list[list[float]]:
    raise NotImplementedError
