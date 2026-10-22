"""미리 계산한 임베딩 파일(.npz). 여러 PC가 나눠 계산한 벡터를 한 곳에서 재사용하기 위한 것이다.

파일 안: keys(텍스트 해시) / vectors(float32, N x D) / meta(모델·최대 길이·차원).
키는 모델명·kind(passage/query)·원문 텍스트의 SHA-256이라, 같은 모델·같은 텍스트면 어느 PC에서 만들었든 같은 키가 된다.
모델이나 최대 길이가 다른 파일은 섞이지 않도록 불러올 때 거부한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np

FORMAT_VERSION = 1
CONFLICT_ATOL = 1e-3        # 같은 키의 벡터가 이보다 크게 다르면 서로 다른 모델·설정으로 만든 것으로 본다 (GPU/CPU 부동소수 차이는 통과)


def text_key(model: str, kind: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{kind}\n{text}".encode("utf-8")).hexdigest()


class VectorCache:
    def __init__(self, model: str, max_seq_length: int, dim: int):
        self.meta = {"format": FORMAT_VERSION, "model": model, "max_seq_length": int(max_seq_length), "dim": int(dim)}
        self._keys: list[str] = []
        self._vectors = np.zeros((0, int(dim)), dtype=np.float32)
        self._index: dict[str, int] = {}
        self._pending: list[tuple[str, np.ndarray]] = []   # add()한 것 (save/조회 시 합친다)

    # --- 조회 ---------------------------------------------------------------
    def _flush(self) -> None:
        if not self._pending:
            return
        new = [(k, v) for k, v in self._pending if k not in self._index]
        self._pending = []
        if not new:
            return
        start = len(self._keys)
        self._keys.extend(k for k, _ in new)
        self._vectors = np.vstack([self._vectors, np.asarray([v for _, v in new], dtype=np.float32)])
        for offset, (k, _) in enumerate(new):
            self._index[k] = start + offset

    def __len__(self) -> int:
        self._flush()
        return len(self._keys)

    def __contains__(self, key: str) -> bool:
        self._flush()
        return key in self._index

    def get(self, key: str) -> np.ndarray | None:
        self._flush()
        i = self._index.get(key)
        return None if i is None else self._vectors[i]

    def keys(self) -> list[str]:
        self._flush()
        return list(self._keys)

    # --- 추가·병합 ----------------------------------------------------------
    def add(self, key: str, vector) -> None:
        vector = np.asarray(vector, dtype=np.float32)
        if vector.shape != (self.meta["dim"],):
            raise ValueError(f"벡터 차원이 다릅니다: {vector.shape} (기대 {self.meta['dim']})")
        existing = self.get(key)
        if existing is not None:
            if not np.allclose(existing, vector, atol=CONFLICT_ATOL):
                raise ValueError("같은 텍스트의 벡터가 서로 다릅니다 (모델·설정이 다른 PC에서 만든 파일일 수 있음)")
            return
        self._pending.append((key, vector))

    def update(self, other: "VectorCache") -> None:
        check_compatible(self.meta, other.meta)
        other._flush()
        for k, v in zip(other._keys, other._vectors):
            self.add(k, v)

    def check_model(self, model: str, max_seq_length: int) -> None:
        if self.meta["model"] != model or self.meta["max_seq_length"] != int(max_seq_length):
            raise ValueError(f"embedding_cache_incompatible: 파일은 {self.meta['model']} (max_seq_length {self.meta['max_seq_length']}), "
                             f"현재 설정은 {model} ({max_seq_length})")

    # --- 저장·불러오기 ---------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        """원자적으로 저장한다 (임시 파일에 쓴 뒤 교체). 중간에 끊겨도 이전 파일이 남는다."""
        self._flush()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            np.savez_compressed(f, keys=np.array(self._keys, dtype=str), vectors=self._vectors,
                                meta=np.array(json.dumps(self.meta)))
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "VectorCache":
        data = np.load(Path(path), allow_pickle=False)
        meta = json.loads(str(data["meta"]))
        if meta.get("format") != FORMAT_VERSION:
            raise ValueError(f"지원하지 않는 임베딩 파일 형식입니다: {meta.get('format')}")
        cache = cls(meta["model"], meta["max_seq_length"], meta["dim"])
        keys = [str(k) for k in data["keys"]]
        vectors = np.asarray(data["vectors"], dtype=np.float32)
        if vectors.shape != (len(keys), meta["dim"]):
            raise ValueError("임베딩 파일이 손상되었습니다 (키 수와 벡터 수가 다름)")
        cache._keys, cache._vectors = keys, vectors
        cache._index = {k: i for i, k in enumerate(keys)}
        return cache

    @classmethod
    def load_many(cls, source: str | Path) -> "VectorCache":
        """파일 하나 또는 폴더(안의 *.npz 전부)를 합쳐서 불러온다. 서로 호환되지 않으면 오류."""
        source = Path(source)
        files = sorted(source.glob("*.npz")) if source.is_dir() else [source]
        if not files:
            raise FileNotFoundError(f"임베딩 파일이 없습니다: {source}")
        merged = cls.load(files[0])
        for f in files[1:]:
            merged.update(cls.load(f))
        return merged


def check_compatible(a: dict, b: dict) -> None:
    for key in ("model", "max_seq_length", "dim"):
        if a.get(key) != b.get(key):
            raise ValueError(f"임베딩 파일의 {key}가 서로 다릅니다: {a.get(key)!r} / {b.get(key)!r}")
