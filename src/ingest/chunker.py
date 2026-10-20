"""구조 분할. max_chars는 서비스명과 개행을 포함한 전체 text 한도다."""
from __future__ import annotations

import re
from src.common import config
from src.common.schema import SECTIONS, Chunk

SECTION_FIELDS = dict(zip(SECTIONS, ("support_target", "selection_criteria", "support_content", "how_to_apply")))


def split_long_text(text: str, max_chars: int) -> list[str]:
    """문단 → 줄 → 문장 → 고정 길이. 구분자도 보존: ''.join(parts) == text."""
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")

    def split(value: str, level: int) -> list[str]:
        if len(value) <= max_chars:
            return [value] if value else []
        boundaries = (r"\n\s*\n", r"\n", r"(?<=[.!?。！？])\s+")
        if level == len(boundaries):
            return [value[i:i + max_chars] for i in range(0, len(value), max_chars)]
        pieces, start = [], 0
        for match in re.finditer(boundaries[level], value):
            pieces.append(value[start:match.end()])
            start = match.end()
        pieces.append(value[start:])
        output, buffer = [], ""
        for piece in pieces:
            for small in split(piece, level + 1):
                if buffer and len(buffer) + len(small) > max_chars:
                    output.append(buffer)
                    buffer = ""
                buffer += small
        if buffer:
            output.append(buffer)
        return output

    return split(text, 0)


def chunk_service(service: dict, max_chars: int = config.MAX_CHUNK_CHARS) -> list[Chunk]:
    from src.ingest.store import build_metadata

    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    sid, name = service["service_id"], service["service_name"]
    if not sid or not name:
        raise ValueError("service ID and name required")
    prefix = name + "\n"
    budget = max_chars - len(prefix)
    if budget < 1:
        raise ValueError("max_chars must leave room after service-name prefix")
    chunks = []
    for section, field in SECTION_FIELDS.items():
        text = service.get(field) or ""
        if not text.strip():
            continue
        parts = split_long_text(text, budget)
        for n, part in enumerate(parts, 1):
            suffix = f"-{n}" if len(parts) > 1 else ""
            chunks.append(Chunk(f"{sid}#{section}{suffix}", sid, name, section,
                                prefix + part, build_metadata(service), service.get("detail_url") or ""))
    return chunks
