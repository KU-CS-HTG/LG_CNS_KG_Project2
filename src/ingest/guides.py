"""판정 기준 문서(고시·업무처리기준·사업안내 PDF) -> GuideChunk (X1, CLAUDE.md 판정 기준 문서 경로).

보조금24 경로(Chunk·store·handoff)와 분리된 새 경로다. 문서 목록은 guide_sources.json(커밋), 원본 PDF는
data/guides/(커밋 금지). 파싱 전에 sha256으로 팀원이 같은 파일을 가졌는지 확인하고, 다르면 멈춘다.
- 조각은 한 쪽 안에서만 자른다 (쪽 번호 = PDF 파일의 쪽 순서, 1부터. eval/guide_questions.jsonl과 같은 기준).
- 여러 쪽에 반복되는 머리말·꼬리말('| 2026년 주거급여 사업안내 |', '- 3 -')은 지운다.
- 소제목은 줄 모양으로 추정해 "문서명 > 장 > 절"로 붙인다 (검색 접두, C의 연혁 판별).
결과는 data/guides/guide_chunks.jsonl (B의 guide_store가 읽는다).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from src.common import config
from src.common.schema import GuideChunk

SOURCES_PATH = Path(__file__).with_name("guide_sources.json")
GUIDES_DIR = config.ROOT / "data" / "guides"
CHUNKS_NAME = "guide_chunks.jsonl"
MAX_CHARS = 900          # bge-m3 512토큰 안에 들어가는 길이 (보조금24 조각 MAX_CHUNK_CHARS 1000자보다 조금 짧게)
OVERLAP_CHARS = 150      # 조각 경계에 걸린 문장을 양쪽 조각에서 찾을 수 있게 앞 조각 끝을 겹친다
MIN_CHARS = 20           # 이보다 짧은 조각(빈 쪽·표 조각)은 버린다
REPEAT_RATIO = 0.3       # 이 비율 이상의 쪽에 나오는 줄은 머리말·꼬리말로 본다 (5쪽 이상 문서만)

_PAGE_NO_RE = re.compile(r"^[\s\-–❙|]*\d{1,4}[\s\-–❙|]*$")
_DIGITS_RE = re.compile(r"\d+")
# 큰 제목: '제2장 …', 'Ⅲ. …', '1 취업지원 이관'·'2 Ⅰ유형 요건'(숫자 뒤 공백) / 작은 제목: '1. 의미', '가. 수선유지급여'
_MAJOR_RE = re.compile(r"^(?:제\s*\d+\s*[장편절]\s*\S|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\s*[.\s]\s*\S|\d{1,2}\s+[가-힣ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ])")
_MINOR_RE = re.compile(r"^(?:\d{1,2}\.\s*[가-힣]|[가-하]\.\s*[가-힣])")
_HEADING_MAX = 30
# 목차·참조 줄은 제목이 아니다: 'Ⅲ-4. 재참여제한 119p 참고', '제2장 소득인정액 ···· 35'
_NOT_HEADING_RE = re.compile(r"\d\s*p\s*참\s*[고조]|[·.…]{2,}|\s\d{1,3}$")
# 글자마다 띄운 제목('신 청 및 선 정 기 준'). 붙여 써야 BM25 2-gram·임베딩이 낱말로 본다 (한 글자 낱말 4개 이상 연속만)
_SPACED_RE = re.compile(r"(?<![가-힣])(?:[가-힣] ){3,}[가-힣](?![가-힣])")


class GuideSourceError(RuntimeError):
    """문서 목록과 data/guides/의 파일이 맞지 않는다 (없음·지문 다름). 다른 파일로 색인하지 않도록 멈춘다."""


def load_sources(path: Path = SOURCES_PATH) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["documents"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(sources: list[dict], guides_dir: Path = GUIDES_DIR) -> None:
    problems = []
    for s in sources:
        p = Path(guides_dir) / s["file"]
        if not p.exists():
            problems.append(f"없음: {s['file']}")
        elif sha256_file(p) != s["sha256"]:
            problems.append(f"지문 다름: {s['file']} (공유 드라이브의 원본과 다른 파일)")
    if problems:
        raise GuideSourceError("판정 기준 문서 확인 실패 - " + "; ".join(problems))


def page_texts(pdf_path: Path) -> list[str]:
    from pypdf import PdfReader
    return [page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages]


def _line_key(line: str) -> str:
    """쪽 번호만 다른 머리말('❙ 104', '❙ 105')을 같은 줄로 보려고 숫자를 지운다."""
    return _DIGITS_RE.sub("#", line.strip())


def repeated_lines(pages: list[str]) -> set[str]:
    if len(pages) < 5:
        return set()
    counts = Counter(k for text in pages for k in {_line_key(l) for l in text.splitlines() if l.strip()})
    return {k for k, n in counts.items() if n >= REPEAT_RATIO * len(pages)}


def clean_lines(text: str, repeated: set[str]) -> list[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or _PAGE_NO_RE.match(s) or _line_key(s) in repeated:
            continue
        s = re.sub(r"[ \t]{2,}", " ", s)
        out.append(_SPACED_RE.sub(lambda m: m.group(0).replace(" ", ""), s))
    return out


def heading_level(line: str) -> int:
    """2 = 큰 제목, 1 = 작은 제목, 0 = 본문."""
    if len(line) > _HEADING_MAX or _NOT_HEADING_RE.search(line):
        return 0
    if _MAJOR_RE.match(line):
        return 2
    if _MINOR_RE.match(line):
        return 1
    return 0


def split_lines(lines: list[str], max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS) -> list[tuple[int, list[str]]]:
    """줄 경계로 max_chars 이하 조각을 만든다. (조각 첫 줄 위치, 줄 목록). 다음 조각은 앞 조각 끝 overlap자를 겹친다."""
    out: list[tuple[int, list[str]]] = []
    i = 0
    while i < len(lines):
        size, j = 0, i
        while j < len(lines) and (j == i or size + len(lines[j]) + 1 <= max_chars):
            size += len(lines[j]) + 1
            j += 1
        out.append((i, lines[i:j]))
        if j >= len(lines):
            break
        back, k = 0, j
        while k - 1 > i and back + len(lines[k - 1]) + 1 <= overlap:
            k -= 1
            back += len(lines[k]) + 1
        i = k
    return out


def doc_chunks(source: dict, pages: list[str]) -> list[GuideChunk]:
    repeated = repeated_lines(pages)
    major = minor = ""
    chunks: list[GuideChunk] = []
    for page_no, text in enumerate(pages, 1):
        lines = clean_lines(text, repeated)
        # 줄마다 그 줄에서 유효한 제목. 조각의 제목은 조각 첫 본문 줄의 제목이다
        # (조각이 '제2장 …', '1. …' 제목 줄로 시작하면 두 제목을 모두 붙인다)
        headings, levels = [], []
        for line in lines:
            level = heading_level(line)
            if level == 2:
                major, minor = line, ""
            elif level == 1:
                minor = line
            levels.append(level)
            headings.append(" > ".join(x for x in (source["title"], major, minor) if x))
        n = 0
        for start, part in split_lines(lines):
            body = "\n".join(part)
            if len(body) < MIN_CHARS:
                continue
            first_body = next((j for j in range(start, start + len(part)) if levels[j] == 0), start + len(part) - 1)
            n += 1
            chunks.append(GuideChunk(
                chunk_id=f"{source['doc_id']}#p{page_no}-{n}", doc_id=source["doc_id"], doc_title=source["title"],
                publisher=source["publisher"], year=int(source["year"]), page=page_no, heading=headings[first_body],
                text=body, scope=source.get("scope") or "전국",
                service_ids=list(source.get("service_ids") or []), source_url=source.get("source_url") or ""))
    return chunks


def build_chunks(sources: list[dict] | None = None, guides_dir: Path = GUIDES_DIR) -> list[GuideChunk]:
    sources = load_sources() if sources is None else sources
    verify(sources, guides_dir)
    out: list[GuideChunk] = []
    for s in sources:
        out.extend(doc_chunks(s, page_texts(Path(guides_dir) / s["file"])))
    return out


def write_chunks(chunks: list[GuideChunk], path: Path) -> None:
    Path(path).write_text("".join(json.dumps(c.to_dict(), ensure_ascii=False) + "\n" for c in chunks), encoding="utf-8")


def read_chunks(path: Path) -> list[GuideChunk]:
    return [GuideChunk(**json.loads(l)) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="판정 기준 문서 PDF -> 조각 (data/guides/guide_chunks.jsonl)")
    ap.add_argument("--dir", default=str(GUIDES_DIR), help="PDF가 있는 폴더 (기본 data/guides)")
    ap.add_argument("--out", help=f"결과 파일 (기본 <dir>/{CHUNKS_NAME})")
    args = ap.parse_args(argv)
    chunks = build_chunks(guides_dir=Path(args.dir))
    out = Path(args.out) if args.out else Path(args.dir) / CHUNKS_NAME
    write_chunks(chunks, out)
    for doc_id, n in Counter(c.doc_id for c in chunks).items():
        print(f"{doc_id}: {n}개")
    print(f"모두 {len(chunks)}개 -> {out}")


if __name__ == "__main__":
    main()
