"""[A/B] SQLite 저장소: 서비스(정형 조건)와 청크 원문을 보관하고 조건 필터를 SQL로 처리한다."""
import sqlite3
from contextlib import closing

from src.config import DB_PATH
from src.regions import NATIONWIDE
from src.schema import Conditions, Service

SERVICE_COLUMNS = list(Service.__annotations__)


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise RuntimeError(f"DB가 없습니다: {DB_PATH}\n먼저 scripts/build_services.py 를 실행하세요.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create(services: list[Service], chunks: list[dict]) -> None:
    """DB를 새로 만든다 (기존 파일은 덮어씀)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    cols = ", ".join(f"{c} {'INTEGER' if c in ('min_age', 'max_age') else 'TEXT'}" for c in SERVICE_COLUMNS)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(f"CREATE TABLE services ({cols}, PRIMARY KEY (service_id))")
        conn.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, service_id TEXT, field TEXT, text TEXT)")
        conn.execute("CREATE INDEX idx_chunks_sid ON chunks(service_id)")
        placeholders = ", ".join("?" * len(SERVICE_COLUMNS))
        conn.executemany(
            f"INSERT OR REPLACE INTO services VALUES ({placeholders})",
            [tuple(s.get(c, "") for c in SERVICE_COLUMNS) for s in services],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?)",
            [(c["chunk_id"], c["service_id"], c["field"], c["text"]) for c in chunks],
        )


def condition_sql(cond: Conditions) -> tuple[str, list]:
    """조건 → WHERE 절. 값이 None인 조건은 넣지 않는다(소프트 필터).

    나이 조건이 없는 서비스는 min_age=0, max_age=200 으로 저장돼 있어 자동으로 통과한다.
    """
    clauses, params = [], []
    if cond.age is not None:
        clauses.append("min_age <= ? AND max_age >= ?")
        params += [cond.age, cond.age]
    if cond.region:
        clauses.append("region IN (?, ?)")
        params += [cond.region, NATIONWIDE]
    return (" AND ".join(clauses) or "1=1"), params


def eligible_ids(cond: Conditions) -> set[str]:
    where, params = condition_sql(cond)
    with closing(connect()) as conn:
        return {r["service_id"] for r in conn.execute(f"SELECT service_id FROM services WHERE {where}", params)}


def get_services(ids: list[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    with closing(connect()) as conn:
        rows = conn.execute(f"SELECT * FROM services WHERE service_id IN ({marks})", ids).fetchall()
    return {r["service_id"]: r for r in rows}


def get_chunks(ids: list[str]) -> list[sqlite3.Row]:
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    with closing(connect()) as conn:
        return conn.execute(f"SELECT * FROM chunks WHERE service_id IN ({marks})", ids).fetchall()


def categories() -> list[str]:
    """DB에 실제로 있는 서비스분야 목록 (조건 추출 프롬프트에 선택지로 제공)."""
    with closing(connect()) as conn:
        rows = conn.execute("SELECT DISTINCT category FROM services WHERE category != ''").fetchall()
    found = set()
    for r in rows:
        found.update(c.strip() for c in r["category"].split(","))   # 'A, B'처럼 여러 분야인 경우
    return sorted(c for c in found if c)


def all_chunks() -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM chunks").fetchall()


def keyword_search(keywords: list[str], cond: Conditions, limit: int) -> list[tuple[str, int]]:
    """[SQL 모드] 조건 필터 + LIKE 키워드 매칭. 매칭된 키워드 수가 많은 순.

    파라미터 바인딩(?)만 사용해 SQL 인젝션을 막는다.
    """
    if not keywords:
        return []
    where, params = condition_sql(cond)
    text_expr = "(name || ' ' || summary || ' ' || target || ' ' || criteria || ' ' || content)"
    score_expr = " + ".join(f"(CASE WHEN {text_expr} LIKE ? THEN 1 ELSE 0 END)" for _ in keywords)
    sql = (f"SELECT service_id, ({score_expr}) AS score FROM services "
           f"WHERE {where} AND ({score_expr}) > 0 ORDER BY score DESC LIMIT ?")
    likes = [f"%{k}%" for k in keywords]
    with closing(connect()) as conn:
        rows = conn.execute(sql, likes + params + likes + [limit]).fetchall()
    return [(r["service_id"], r["score"]) for r in rows]
