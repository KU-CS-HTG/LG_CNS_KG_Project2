"""덧씌운 변경을 검색 색인(Chroma)에 반영한다. 반영하기 전에도 '숨김'은 답변에서 바로 빠진다 (generator의 거르기).

- 숨긴 제도·삭제한 직접 추가 제도: 색인에서 지운다.
- 직접 추가·수정한 제도, 숨김을 푼 제도: 최신 값으로 조각을 다시 만들어 넣는다 (임베딩 모델이 필요하다).
다시 실행해도 같은 결과다 (조각 ID 기준 upsert). A의 수집이 같은 제도를 다시 색인하면 수정이 덮어써질 수 있으니
수집 뒤에는 다시 반영한다. 이 경로는 handoff의 잠금·확인(ack)을 거치지 않는 B의 직접 색인이다."""
from __future__ import annotations

from src.catalog.effective import effective_service
from src.catalog.store import Overlay


def plan(api_services: dict[str, dict], overlay: Overlay) -> dict:
    remove, upsert, skipped = [], [], []
    for sid in sorted(overlay.touched):
        if sid in overlay.removed or overlay.is_hidden(sid):
            remove.append(sid)
            continue
        record = effective_service(sid, api_services.get(sid), overlay)
        (upsert if record is not None else skipped).append(sid)
    return {"remove": remove, "upsert": upsert, "skipped": skipped}


def apply(api_services: dict[str, dict], overlay: Overlay, *, dry_run: bool = False) -> dict:
    from src.ingest import chunker

    p = plan(api_services, overlay)
    report = {"remove": len(p["remove"]), "upsert": len(p["upsert"]), "chunks": 0, "skipped": p["skipped"], "dry_run": dry_run}
    chunks = []
    for sid in p["upsert"]:
        chunks += chunker.chunk_service(effective_service(sid, api_services.get(sid), overlay))
    report["chunks"] = len(chunks)
    if dry_run:
        return report
    from src.index import vector_store                    # 모듈 속성으로 호출 (테스트에서 바꿔 끼움)

    if p["remove"]:
        vector_store.remove_services(p["remove"])
    if chunks:
        vector_store.build_index(chunks)
    return report
