"""새 질문 일괄 등록: 스프레드시트(csv)나 jsonl -> question 이벤트. 이미 있는 질문(정규화한 문장이 같음)은 건너뛴다.

열 이름(한글도 가능): question|질문(필수), category|범주, topic|주제, age|나이, region|지역, field|분야, situation|상황,
tags|태그(; 로 구분), expected_answer|기대답변, source|출처(기본 team), paraphrase_of, split.
나이·지역·분야·상황은 질문 문장에서 정답을 정할 때 쓰는 기대 조건(gold_filters)이 된다 (값이 비면 생략)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from src.review import schema
from src.review.state import State
from src.review.store import ReviewStore

ALIASES = {"질문": "question", "범주": "category", "주제": "topic", "나이": "age", "지역": "region", "분야": "field",
           "상황": "situation", "태그": "tags", "기대답변": "expected_answer", "출처": "source"}


def read_rows(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:      # 엑셀이 저장한 BOM 허용
            raw = list(csv.DictReader(f))
    else:
        raw = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    return [{ALIASES.get(str(k).strip(), str(k).strip()): v for k, v in row.items() if k} for row in raw]


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def parse_row(row: dict) -> tuple[str, dict]:
    """한 행 -> (질문 문장, add_question 인자). 형식이 틀리면 ValueError."""
    text = _clean(row.get("question"))
    if not text:
        raise ValueError("질문이 비어 있습니다")
    fields: dict = {}
    for key in ("category", "topic", "expected_answer", "paraphrase_of"):
        if _clean(row.get(key)):
            fields[key] = _clean(row[key])
    gold = {}
    for key in ("region", "field", "situation"):
        if _clean(row.get(key)):
            gold[key] = _clean(row[key])
    if _clean(row.get("age")):
        try:
            gold["age"] = int(float(_clean(row["age"])))
        except ValueError:
            raise ValueError(f"나이가 숫자가 아닙니다: {row['age']!r}") from None
    if gold:
        fields["gold_filters"] = gold
    tags = row.get("tags")
    tags = tags if isinstance(tags, list) else [t.strip() for t in _clean(tags).split(";") if t.strip()]
    if tags:
        fields["tags"] = tags
    split = _clean(row.get("split"))
    if split:
        fields["split"] = split
    fields["_source"] = _clean(row.get("source")) or "team"
    return text, fields


def import_questions(store: ReviewStore, path: str | Path, reviewer: str, *, state: State | None = None, dry_run: bool = False) -> dict:
    """등록한 개수와 건너뛴 이유를 돌려준다. 다시 실행해도 중복되지 않는다. 형식 오류는 한 줄씩 모아 보여 주고 나머지는 계속 처리한다."""
    state = state or store.state()
    seen = set(state.questions)
    report = {"added": 0, "existing": 0, "duplicate_in_file": 0, "invalid": [], "by_split": {"dev": 0, "test": 0}}
    for line_no, row in enumerate(read_rows(path), start=1):
        try:
            text, fields = parse_row(row)
            source = fields.pop("_source")
            qid = schema.question_id(text)
            schema.validate_event(schema.make_event("question", {"question_id": qid, "text": text, "source": source, **fields}, reviewer))
        except (ValueError, schema.ValidationError) as exc:
            report["invalid"].append(f"{line_no}행: {exc}")
            continue
        if qid in seen:
            report["existing" if qid in state.questions else "duplicate_in_file"] += 1
            continue
        seen.add(qid)
        report["by_split"][fields.get("split") or schema.default_split(qid)] += 1
        report["added"] += 1
        if not dry_run:
            store.add_question(reviewer, text, source=source, client_event_id=f"intake:{qid}", **fields)
    return report
