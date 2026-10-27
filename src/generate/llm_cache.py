"""같은 LLM 입력이면 저장해 둔 응답을 다시 쓴다 (같은 질문을 반복하면 같은 답, 2026-10-27 요청).

temperature=0이어도 OpenAI 응답은 매번 조금씩 달라, 조건 추출이 흔들리면 검색 결과와 카드가 통째로 바뀐다.
키 = (모델, 시스템 프롬프트, 사용자 프롬프트)의 해시. 근거 문구(데이터 팩)·프롬프트가 바뀌면 키가 달라져 새로 묻는다.
- 공유 파일(SHARED_PATH, 커밋): 평가·시연 질문의 응답. 데이터 팩이 같으면 모든 PC가 같은 답을 낸다.
  신청 링크 표처럼 한 사람만 `python -m src.generate.llm_cache warm`으로 갱신한다 (충돌 방지).
- 로컬 DB(LOCAL_PATH, git 제외): 그 밖의 질문. 그 PC에서는 두 번째부터 같은 답.
LLM_CACHE=0이면 읽지도 쓰지도 않는다 (평가에서 새 응답을 받고 싶을 때).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from src.common import config

log = logging.getLogger(__name__)

SHARED_PATH = Path(__file__).with_name("llm_cache.jsonl")
LOCAL_PATH = config.ROOT / "data" / "llm_cache.db"
# OpenAI seed: 처음 묻는 질문의 흔들림을 줄인다 (같은 답 보장은 아님. 보장은 저장된 응답으로 한다)
SEED = 7
DEFAULT_QUESTIONS = (config.EVAL_DIR / "questions.jsonl", config.EVAL_DIR / "demo_questions.jsonl")

_WS_RE = re.compile(r"\s+")
_shared: dict[str, str] | None = None   # key -> 응답 JSON 문자열 (처음 쓸 때 읽는다)
_used: set[str] | None = None           # warm 중에 쓰인 키 (공유 파일에 넣을 것)
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("LLM_CACHE", "1") != "0"


def key(model: str, system: str, user: str) -> str:
    """공백 차이('수원  살아요', 앞뒤 줄바꿈)는 같은 입력으로 본다."""
    norm = [_WS_RE.sub(" ", s).strip() for s in (model, system, user)]
    return hashlib.sha256(json.dumps(norm, ensure_ascii=False).encode("utf-8")).hexdigest()


def _load_shared() -> dict[str, str]:
    """조건 추출·판정은 여러 스레드에서 동시에 부른다. 다 읽은 뒤에 넘겨야 다른 스레드가 읽는 중인
    (덜 찬) 목록을 보고 '없음'으로 판단하지 않는다."""
    global _shared
    with _lock:
        if _shared is None:
            rows: dict[str, str] = {}
            if SHARED_PATH.exists():
                for line in SHARED_PATH.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        rows[row["key"]] = json.dumps(row["data"], ensure_ascii=False)
            _shared = rows
        return _shared


def _connect() -> sqlite3.Connection:
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LOCAL_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model TEXT, data TEXT, created TEXT)")
    return conn


def get(model: str, system: str, user: str) -> dict | None:
    """저장된 응답(새 dict) 또는 None. 공유 파일을 먼저 본다 (PC마다 같은 답이 우선)."""
    if not enabled():
        return None
    k = key(model, system, user)
    text = _load_shared().get(k)
    if text is None:
        try:
            with _connect() as conn:
                row = conn.execute("SELECT data FROM llm_cache WHERE key = ?", (k,)).fetchone()
        except sqlite3.Error as e:  # 저장소 문제로 답변이 멈추지 않게 그냥 LLM을 부른다
            log.warning("LLM 캐시 읽기 실패: %s", e)
            return None
        text = row[0] if row else None
    if text is not None and _used is not None:
        _used.add(k)
    return json.loads(text) if text is not None else None


def put(model: str, system: str, user: str, data: dict) -> None:
    """성공한 응답만 로컬 DB에 저장한다 (실패·오류는 호출한 쪽에서 저장하지 않는다)."""
    if not enabled():
        return
    k = key(model, system, user)
    try:
        with _connect() as conn:
            conn.execute("INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?, ?)",
                         (k, model, json.dumps(data, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
    except sqlite3.Error as e:
        log.warning("LLM 캐시 저장 실패: %s", e)
        return
    if _used is not None:
        _used.add(k)


def _local_rows(keys: set[str]) -> dict[str, tuple[str, str]]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, model, data FROM llm_cache").fetchall()
    return {k: (m, d) for k, m, d in rows if k in keys}


def write_shared(keys: set[str], fresh: bool = False) -> int:
    """keys의 응답을 공유 파일에 쓴다. fresh가 아니면 기존 공유 항목도 남긴다. 키 순서로 한 줄에 하나 (diff가 작게)."""
    global _shared
    rows: dict[str, dict] = {}
    if not fresh and SHARED_PATH.exists():
        for line in SHARED_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["key"]] = row
    shared = _load_shared()
    local = _local_rows(keys)
    for k in keys:
        if k in local:
            rows[k] = {"key": k, "model": local[k][0], "data": json.loads(local[k][1])}
        elif k in shared and k not in rows:
            rows[k] = {"key": k, "model": config.GENERATION_MODEL, "data": json.loads(shared[k])}
    SHARED_PATH.write_text("".join(json.dumps(rows[k], ensure_ascii=False) + "\n" for k in sorted(rows)),
                           encoding="utf-8")
    _shared = None
    return len(rows)


def _questions(paths) -> list[str]:
    out: list[str] = []
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            if line.strip():
                q = json.loads(line).get("question", "")
                if q and q not in out:
                    out.append(q)
    return out


def warm(paths=DEFAULT_QUESTIONS, fresh: bool = False) -> int:
    """질문 파일의 질문마다 답변을 한 번 만들어, 그때 쓰인 LLM 응답을 공유 파일에 넣는다 (색인·OPENAI_API_KEY 필요)."""
    global _used
    from src.generate import generator
    questions = _questions(paths)
    _used = set()
    try:
        for i, q in enumerate(questions, 1):
            print(f"[{i}/{len(questions)}] {q}")
            generator.answer_question(q)
        keys = set(_used)
    finally:
        _used = None
    return write_shared(keys, fresh=fresh)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="LLM 응답 저장소 (같은 질문이면 같은 답)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("warm", help="평가·시연 질문을 돌려 공유 파일(커밋용)을 만든다. 한 사람만 실행")
    w.add_argument("files", nargs="*", default=[str(p) for p in DEFAULT_QUESTIONS])
    w.add_argument("--fresh", action="store_true", help="기존 공유 항목을 버리고 이번에 쓰인 응답만 남긴다")
    sub.add_parser("stats", help="공유·로컬 저장 개수")
    args = ap.parse_args(argv)
    if args.cmd == "warm":
        if not enabled():
            raise SystemExit("LLM_CACHE=0 상태에서는 warm을 할 수 없습니다.")
        n = warm(args.files, fresh=args.fresh)
        print(f"공유 파일 {SHARED_PATH.relative_to(config.ROOT)}: {n}개")
    else:
        with _connect() as conn:
            local = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        print(f"공유 {len(_load_shared())}개 ({SHARED_PATH.name}), 로컬 {local}개 ({LOCAL_PATH})")


if __name__ == "__main__":
    # python -m으로 실행하면 이 파일이 __main__으로 한 번 더 올라온다. condition_extractor가 쓰는 모듈의
    # _used에 기록되도록 패키지 모듈의 main을 부른다 (그렇지 않으면 warm이 0개를 쓴다)
    from src.generate import llm_cache as _module
    _module.main()
