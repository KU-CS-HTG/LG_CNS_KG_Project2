"""답변 선정 과정을 터미널(stderr)에 보여 주는 추적. 꺼져 있으면 아무것도 하지 않는다.

켜는 법: 환경변수 RAG_TRACE=1 (앱을 띄운 터미널에 나온다), 또는 `python -m src.generate.explain "질문"`.
  PowerShell:  $env:RAG_TRACE = "1"; python -m app.main
각 단계에서 어떤 제도가 남고 빠졌는지와 그 이유를 한 줄씩 적는다. 답변 내용은 바꾸지 않는다."""
from __future__ import annotations

import os
import sys
from collections.abc import Iterable

from src.common.schema import Chunk

_forced: bool | None = None
NAME_CHARS = 36


def enabled() -> bool:
    if _forced is not None:
        return _forced
    return os.environ.get("RAG_TRACE", "").strip().lower() in ("1", "true", "yes", "on")


def set_enabled(value: bool | None) -> None:
    """테스트·CLI에서 환경변수와 상관없이 켜고 끈다. None이면 환경변수를 따른다."""
    global _forced
    _forced = value


def out(line: str = "") -> None:
    if enabled():
        print(line, file=sys.stderr, flush=True)


def title(text: str) -> None:
    out(f"\n[{text}]")


def short(name: str) -> str:
    return name if len(name) <= NAME_CHARS else name[:NAME_CHARS - 1] + "…"


def ids(chunks: Iterable[Chunk]) -> list[str]:
    seen: list[str] = []
    for c in chunks:
        if c.service_id not in seen:
            seen.append(c.service_id)
    return seen


def names(chunks: Iterable[Chunk]) -> dict[str, str]:
    return {c.service_id: c.service_name for c in chunks}


def listing(label: str, chunks: list[Chunk]) -> None:
    """제도 목록(서비스당 한 줄)."""
    if not enabled():
        return
    title(f"{label} {len(ids(chunks))}개")
    nm = names(chunks)
    for i, sid in enumerate(ids(chunks), start=1):
        out(f"  {i:>2}. {sid}  {short(nm[sid])}")


def dropped(reason: str, before: list[Chunk], after: list[Chunk]) -> None:
    """before에는 있고 after에는 없는 제도를 이유와 함께 적는다. 빠진 것이 없으면 아무것도 적지 않는다."""
    if not enabled():
        return
    keep = set(ids(after))
    gone = [sid for sid in ids(before) if sid not in keep]
    if gone:
        nm = names(before)
        title(f"제외: {reason} ({len(gone)}개)")
        for sid in gone:
            out(f"  - {sid}  {short(nm[sid])}")


def decision(sid: str, name: str, verdict: str, reason: str) -> None:
    out(f"  {verdict:<4} {sid}  {short(name)}  <- {reason}")
