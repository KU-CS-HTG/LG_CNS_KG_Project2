"""① 로드 - 보조금24 API 호출. 원본 응답은 data/raw/에 저장(커밋 금지)."""
from __future__ import annotations

import json
from collections.abc import Iterator

import requests

from src.common import config


def _get(endpoint: str, **params) -> dict:
    # requests가 인코딩하므로 인증키는 Decoding 키를 사용한다.
    resp = requests.get(
        f"{config.GOV24_API_BASE}/{endpoint}",
        params={"serviceKey": config.GOV24_API_KEY, **params},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_all(endpoint: str, per_page: int = 500) -> Iterator[dict]:
    """endpoint(serviceList | serviceDetail | supportConditions)의 전체 행을 순회."""
    page = 1
    while True:
        data = _get(endpoint, page=page, perPage=per_page, returnType="JSON")
        rows = data.get("data", [])
        if not rows:
            return
        yield from rows
        if page * per_page >= data.get("totalCount", 0):
            return
        page += 1


def save_raw(endpoint: str, rows: list[dict]) -> None:
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RAW_DIR / f"{endpoint}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
