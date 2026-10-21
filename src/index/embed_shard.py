"""임베딩 계산을 여러 PC가 나눠서 하기 (색인 적용은 한 곳에서). 사용법은 아래 명령 참고.

흐름: 같은 DB 사본으로 각자 자기 구간만 계산 -> 파일 취합 -> 주 PC에서 기존 핸드오프를 그대로 실행.
이 모듈은 DB·색인을 쓰지 않는다 (DB는 읽기 전용으로 연다). 색인 적용과 확인(ack)은 `python -m src.ingest.handoff --apply`가 한다.

  python -m src.index.embed_shard run --shard 0/3 --out data/embed_shards/part0.npz     # 내 구간만 계산 (이어하기 가능)
  python -m src.index.embed_shard merge part0.npz part1.npz part2.npz --out data/embed_shards/all.npz
  python -m src.index.embed_shard coverage data/embed_shards                           # 대기 중인 조각 중 몇 개가 계산돼 있는지
  $env:EMBEDDING_CACHE = "data/embed_shards"; python -m src.ingest.handoff --apply     # 주 PC: 계산된 벡터를 쓰며 적용

서비스는 service_id의 해시로 구간이 정해진다 (PC마다 같은 DB면 같은 결과, 구간끼리 겹치지 않음).
같은 텍스트는 한 번만 계산한다. 모델·max_seq_length·sentence-transformers 버전이 모든 PC에서 같아야 한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from src.common import config
from src.index import embed_cache

DEFAULT_DB = config.DB_PATH


def load_pending(db_path: str | Path) -> list[tuple[str, list[str]]]:
    """(service_id, 조각 텍스트 목록). 제거 이벤트는 건너뛴다. DB는 읽기 전용으로 연다."""
    db = Path(db_path)
    if not db.is_file():
        raise SystemExit(f"DB가 없습니다: {db}")
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        rows = con.execute("SELECT service_id, action, chunks FROM pending_changes ORDER BY service_id").fetchall()
    out = []
    for service_id, action, chunks in rows:
        if action == "remove":
            continue
        out.append((service_id, [c["text"] for c in json.loads(chunks)]))
    return out


def shard_of(service_id: str, total: int) -> int:
    return int(hashlib.sha256(service_id.encode("utf-8")).hexdigest()[:8], 16) % total


def parse_shard(text: str) -> tuple[int, int]:
    try:
        index, total = (int(x) for x in text.split("/"))
    except ValueError:
        raise SystemExit(f"--shard는 '번호/전체' 형식이어야 합니다 (예: 0/3): {text!r}")
    if total < 1 or not 0 <= index < total:
        raise SystemExit(f"--shard 번호는 0 이상 전체 미만이어야 합니다: {text!r}")
    return index, total


def shard_texts(pending: list[tuple[str, list[str]]], index: int, total: int) -> list[str]:
    """이 구간의 조각 텍스트 (중복 제거, 순서 유지)."""
    seen: dict[str, None] = {}
    for service_id, texts in pending:
        if shard_of(service_id, total) == index:
            for t in texts:
                seen.setdefault(t, None)
    return list(seen)


def compute(texts: list[str], out: Path, *, batch: int, save_every: int, embed_fn=None, log=print) -> embed_cache.VectorCache | None:
    """texts 중 out 파일에 없는 것만 계산해 저장한다. 중간에 끊겨도 저장된 만큼은 남고, 다시 실행하면 이어서 한다."""
    from src.index import embedder

    embed_fn = embed_fn or embedder.embed_texts
    model, max_len = config.EMBEDDING_MODEL, config.EMBEDDING_MAX_SEQ_LENGTH
    if out.exists():
        cache = embed_cache.VectorCache.load(out)
        cache.check_model(model, max_len)
        log(f"[embed_shard] 기존 파일 {len(cache)}개 벡터에서 이어서 계산합니다: {out}")
    else:
        cache = None
    keyed = [(embed_cache.text_key(model, "passage", t), t) for t in texts]
    todo = [(k, t) for k, t in keyed if cache is None or k not in cache]
    log(f"[embed_shard] 구간 텍스트 {len(texts)}개 중 계산할 것 {len(todo)}개 (장치: {config.EMBEDDING_DEVICE or '자동'})")
    started, done_batches = time.time(), 0
    for i in range(0, len(todo), batch):
        part = todo[i : i + batch]
        vectors = embed_fn([t for _, t in part], kind="passage")
        if cache is None:
            cache = embed_cache.VectorCache(model, max_len, len(vectors[0]))
        for (k, _), v in zip(part, vectors):
            cache.add(k, v)
        done_batches += 1
        if done_batches % save_every == 0:
            cache.save(out)
            done = min(i + batch, len(todo))
            rate = done / max(time.time() - started, 1e-9)
            log(f"[embed_shard] {done}/{len(todo)} 저장 ({rate:.1f}개/초, 남은 시간 약 {(len(todo) - done) / max(rate, 1e-9) / 60:.0f}분)")
    if cache is None:                          # 계산할 것이 하나도 없었고 파일도 없던 경우
        log("[embed_shard] 계산할 텍스트가 없습니다")
        return None
    cache.save(out)
    log(f"[embed_shard] 완료: {len(cache)}개 벡터 -> {out}")
    return cache



def merge(files: list[str | Path], out: Path, log=print) -> embed_cache.VectorCache:
    merged = embed_cache.VectorCache.load(files[0])
    for f in files[1:]:
        merged.update(embed_cache.VectorCache.load(f))     # 모델·차원이 다르거나 같은 텍스트의 벡터가 다르면 여기서 오류
    merged.save(out)
    log(f"[embed_shard] 병합 {len(files)}개 파일 -> {len(merged)}개 벡터: {out}")
    return merged


def coverage(db_path: str | Path, source: str | Path, total_shards: int | None = None) -> dict:
    """대기 중인 조각 텍스트가 계산된 벡터 파일에 얼마나 들어 있는지 (구간 수를 주면 구간별로도)."""
    cache = embed_cache.VectorCache.load_many(source)
    cache.check_model(config.EMBEDDING_MODEL, config.EMBEDDING_MAX_SEQ_LENGTH)
    pending = load_pending(db_path)
    model = config.EMBEDDING_MODEL
    all_texts = shard_texts(pending, 0, 1)
    covered = sum(1 for t in all_texts if embed_cache.text_key(model, "passage", t) in cache)
    result = {"unique_texts": len(all_texts), "covered": covered, "missing": len(all_texts) - covered}
    if total_shards:
        result["shards"] = []
        for i in range(total_shards):
            texts = shard_texts(pending, i, total_shards)
            hit = sum(1 for t in texts if embed_cache.text_key(model, "passage", t) in cache)
            result["shards"].append({"shard": f"{i}/{total_shards}", "texts": len(texts), "covered": hit, "missing": len(texts) - hit})
    return result


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="임베딩 계산 분담 (구간 계산 / 병합 / 확인)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="내 구간의 임베딩을 계산해 파일로 저장 (이어하기 가능)")
    p.add_argument("--shard", required=True, help="번호/전체, 예: 0/3")
    p.add_argument("--out", required=True)
    p.add_argument("--db-path", default=str(DEFAULT_DB))
    p.add_argument("--batch", type=int, default=config.INDEX_BATCH_SIZE)
    p.add_argument("--save-every", type=int, default=20, help="이 배치 수마다 파일에 저장")
    p = sub.add_parser("merge", help="구간별 파일을 하나로 합치기")
    p.add_argument("files", nargs="+")
    p.add_argument("--out", required=True)
    p = sub.add_parser("coverage", help="대기 중인 조각 중 계산된 비율 (파일 또는 폴더)")
    p.add_argument("source")
    p.add_argument("--db-path", default=str(DEFAULT_DB))
    p.add_argument("--shards", type=int, help="구간 수를 주면 구간별 현황도 출력")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        index, total = parse_shard(args.shard)
        texts = shard_texts(load_pending(args.db_path), index, total)
        if config.EMBEDDING_CACHE:
            print("[embed_shard] 주의: EMBEDDING_CACHE가 설정돼 있습니다. 계산 중에는 해제하세요.", file=sys.stderr)
        compute(texts, Path(args.out), batch=args.batch, save_every=args.save_every)
    elif args.cmd == "merge":
        merge(args.files, Path(args.out))
    else:
        print(json.dumps(coverage(args.db_path, args.source, args.shards), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
