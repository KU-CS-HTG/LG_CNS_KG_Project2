"""OpenAI 호출 공통부 (조건 추출·답변 생성·답변 평가가 함께 쓴다).

테스트에서는 `llm.chat_json`을 모듈 속성으로 바꿔 끼워 LLM 없이 시험한다.
"""
from __future__ import annotations

import json

from src.common import config

_client = None


def get_client():
    """OpenAI 클라이언트를 처음 쓸 때 만든다 (키가 없으면 import만으로 실패하지 않게)."""
    global _client
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        from openai import OpenAI
        _client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=30)
    return _client


def chat_json(system: str, user: str, model: str = config.GENERATION_MODEL, temperature: float = 0) -> dict:
    """JSON 모드로 호출해 dict를 돌려준다. 응답이 JSON 객체가 아니면 ValueError."""
    resp = get_client().chat.completions.create(
        model=model,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"JSON 객체가 아닌 응답: {data!r}")
    return data
