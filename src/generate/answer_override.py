"""담당자가 제도별 답변 카드를 직접 구성한다 (화면: `python -m app.amount_mode`의 '② 답변 구성').

수집 원문·LLM이 만든 카드 문구가 마음에 들지 않을 때, 카드의 '무엇을 받나요'·'어떻게 신청하나요'·자격 체크리스트·금액 안내를 사람이 직접 쓴 내용으로 바꾼다.
저장하면 그 제도가 실제 답변에 나올 때 `apply`/`find`를 통해 카드에 그대로 쓰인다 (질문마다 달라지는 이유 문구·판정은 건드리지 않는다).

저장 방식은 확인 기록(`amount_reviewed`)과 같다: 입력자별 `src/generate/answer_overrides/<이름>.jsonl`에 이벤트를 추가만 하고(git 충돌 없음),
제도마다 시각이 가장 늦은 이벤트가 이긴다. 'clear' 이벤트로 되돌린다.
금액 안내가 있으면 공식 출처(제목·주소·근거 구절)가 꼭 있어야 한다 (사람이 확인한 금액만 담당자 안내로 나가게)."""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from src.common import config
from src.common.schema import Recommendation

OVERRIDE_DIR = config.ROOT / "src" / "generate" / "answer_overrides"
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
MAX_BENEFIT, MAX_HOW, MAX_NEEDS, MAX_NEED_LEN = 600, 600, 8, 120
MAX_AMOUNTS, MAX_LABEL, MAX_TEXT, MAX_COND, MAX_NOTES, MAX_NOTE = 8, 40, 160, 120, 3, 200
LEVELS = ("공식", "2차")
LAYOUT_MODES, LAYOUT_ORDERS = ("side", "stack"), ("benefit_first", "apply_first")
MIN_LEFT, MAX_LEFT = 20, 80

_preview = threading.local()          # 규칙 관리 화면의 '입력 후' 미리보기 전용 (저장하지 않은 구성)
_cache: dict[str, tuple] = {}


def valid_editor(editor: str) -> bool:
    return bool(_NAME_RE.fullmatch(editor or ""))


def validate(data: dict) -> tuple[dict | None, list[str]]:
    """입력 -> (정리된 구성, 문제 목록). 문제가 있으면 구성은 None. 비어 있는 칸은 '바꾸지 않음'이다."""
    problems: list[str] = []
    benefit = str(data.get("benefit_text") or "").strip()
    how = str(data.get("how_to_apply") or "").strip()
    needs = [str(l).strip() for l in (data.get("needs_check") or []) if str(l).strip()]
    notes = [str(l).strip() for l in (data.get("notes") or []) if str(l).strip()]
    amounts = [{"label": str(a.get("label") or "").strip(), "text": str(a.get("text") or "").strip(), "condition": str(a.get("condition") or "").strip()}
               for a in (data.get("amounts") or []) if isinstance(a, dict) and (str(a.get("label") or "").strip() or str(a.get("text") or "").strip())]
    if len(benefit) > MAX_BENEFIT or len(how) > MAX_HOW:
        problems.append(f"'무엇을 받나요'·'어떻게 신청하나요'는 {MAX_BENEFIT}자 이하로 써 주세요.")
    if len(needs) > MAX_NEEDS or any(len(n) > MAX_NEED_LEN for n in needs):
        problems.append(f"자격 체크리스트는 {MAX_NEEDS}개 이하, 항목마다 {MAX_NEED_LEN}자 이하로 써 주세요.")
    if len(notes) > MAX_NOTES or any(len(n) > MAX_NOTE for n in notes):
        problems.append(f"안내 메모는 {MAX_NOTES}개 이하, 각 {MAX_NOTE}자 이하로 써 주세요.")
    if len(amounts) > MAX_AMOUNTS:
        problems.append(f"금액 안내는 {MAX_AMOUNTS}개 이하로 써 주세요.")
    for i, a in enumerate(amounts, 1):
        if not a["label"] or not a["text"]:
            problems.append(f"금액 안내 {i}: 이름과 금액 문구를 모두 써 주세요.")
        elif len(a["label"]) > MAX_LABEL or len(a["text"]) > MAX_TEXT or len(a["condition"]) > MAX_COND:
            problems.append(f"금액 안내 {i}: 글이 너무 길어요 (이름 {MAX_LABEL}자, 금액 {MAX_TEXT}자, 조건 {MAX_COND}자).")
    src = data.get("source") or {}
    source = {k: str(src.get(k) or "").strip() for k in ("title", "url", "level", "quote")}
    if amounts:
        if not source["title"]:
            problems.append("금액 안내에는 공식 자료의 제목이 필요해요.")
        if not source["url"].startswith(("https://", "http://")):
            problems.append("금액 안내에는 공식 자료의 주소(https://…)가 필요해요.")
        if len(source["quote"]) < 8:
            problems.append("금액 안내에는 자료에서 그대로 복사한 근거 구절이 필요해요.")
        if source["level"] not in LEVELS:
            problems.append("자료 등급(공식/2차)을 골라 주세요.")
    lay = data.get("layout") or {}
    layout = {"mode": lay.get("mode", "side"), "order": lay.get("order", "benefit_first"), "left_pct": lay.get("left_pct", 50)}
    if layout["mode"] not in LAYOUT_MODES or layout["order"] not in LAYOUT_ORDERS or not (
            isinstance(layout["left_pct"], int) and not isinstance(layout["left_pct"], bool) and MIN_LEFT <= layout["left_pct"] <= MAX_LEFT):
        problems.append(f"카드 배치가 올바르지 않아요 (왼쪽 칸 너비는 {MIN_LEFT}~{MAX_LEFT}%).")
    default_layout = layout == {"mode": "side", "order": "benefit_first", "left_pct": 50}
    if not (benefit or how or needs or notes or amounts) and default_layout:
        problems.append("바꿀 내용을 하나 이상 써 주세요.")
    if problems:
        return None, problems
    return {"benefit_text": benefit, "how_to_apply": how, "needs_check": needs, "notes": notes, "amounts": amounts,
            "source": source if amounts else {}, **({} if default_layout else {"layout": layout})}, []


def overrides(folder: Path | None = None) -> dict[str, dict]:
    """제도 -> 현재 구성 (입력자 파일을 모두 읽어 시각이 가장 늦은 이벤트가 이긴다, clear면 빠진다). 파일이 바뀌면 다시 읽는다."""
    folder = folder or OVERRIDE_DIR
    files = sorted(folder.glob("*.jsonl")) if folder.is_dir() else []
    stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    hit = _cache.get(str(folder))
    if hit and hit[0] == stamp:
        return hit[1]
    last: dict[str, dict] = {}
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("service_id") and e.get("action") in ("set", "clear") and e.get("at", "") >= last.get(e["service_id"], {}).get("at", ""):
                last[e["service_id"]] = e
    result = {sid: {**e["data"], "by": e.get("by", ""), "at": e.get("at", "")} for sid, e in last.items() if e["action"] == "set" and isinstance(e.get("data"), dict)}
    _cache[str(folder)] = (stamp, result)
    return result


def _write(service_id: str, editor: str, action: str, data: dict | None, folder: Path | None) -> None:
    if not valid_editor(editor):
        raise ValueError("입력자 이름은 영문·숫자·_-만 쓸 수 있어요.")
    folder = folder or OVERRIDE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    event = {"service_id": service_id, "action": action, "data": data, "by": editor, "at": datetime.now(timezone.utc).isoformat()}
    with (folder / f"{editor}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def save(service_id: str, editor: str, data: dict, folder: Path | None = None) -> None:
    clean, problems = validate(data)
    if clean is None:
        raise ValueError("; ".join(problems))
    _write(service_id, editor, "set", clean, folder)


def clear(service_id: str, editor: str, folder: Path | None = None) -> None:
    _write(service_id, editor, "clear", None, folder)


@contextmanager
def preview(service_id: str, data: dict | None):
    """이 블록 안에서는 find()가 저장하지 않은 구성(data)을 돌려준다 ('입력 후' 미리보기)."""
    previous = getattr(_preview, "value", None)
    _preview.value = (service_id, data)
    try:
        yield
    finally:
        _preview.value = previous


def find(service_id: str) -> dict | None:
    pending = getattr(_preview, "value", None)
    if pending is not None and pending[0] == service_id:
        return pending[1]
    return overrides().get(service_id) if service_id else None


def apply(r: Recommendation) -> Recommendation:
    """저장된 구성이 있으면 카드에 쓰는 문구를 바꾼 사본을 돌려준다 (없으면 그대로). 바꾸는 것: 무엇을 받나요·신청방법·자격 체크리스트."""
    ov = find(r.service_id)
    if not ov:
        return r
    return replace(r, benefit=ov.get("benefit_text") or r.benefit, how_to_apply=ov.get("how_to_apply") or r.how_to_apply,
                   needs_check=list(ov.get("needs_check") or r.needs_check))
