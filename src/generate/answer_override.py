"""담당자가 제도별 답변 카드를 직접 구성한다 (화면: `python -m app.amount_mode`의 '② 답변 구성').

수집 원문·LLM이 만든 카드 문구가 마음에 들지 않을 때, 카드의 '무엇을 받나요'·'어떻게 신청하나요'·자격 체크리스트·금액 안내를 사람이 직접 쓴 내용으로 바꾼다.
저장하면 그 제도가 실제 답변에 나올 때 `apply`/`find`를 통해 카드에 그대로 쓰인다 (질문마다 달라지는 이유 문구·판정은 건드리지 않는다).

저장 방식은 확인 기록(`amount_reviewed`)과 같다: 입력자별 `src/generate/answer_overrides/<이름>.jsonl`에 이벤트를 추가만 하고(git 충돌 없음),
제도마다 시각이 가장 늦은 이벤트가 이긴다. 'clear' 이벤트로 되돌린다.
금액 안내가 있으면 공식 출처(제목·주소·근거 구절)가 꼭 있어야 한다 (사람이 확인한 금액만 담당자 안내로 나가게).

AI가 채운 구성(`ai=True`)은 사람이 '확인 완료'(`confirm`)하거나 고쳐서 다시 저장하기 전에는 실제 답변에 쓰이지 않는다 (`find`는 숨기고 `find_any`만 보여 준다).
AI가 조사한 후보 값·출처는 `ai_memo`에 적어 두며, 사용자 화면에는 나오지 않고 관리 화면에서만 본다."""
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
MAX_SUPPORT, MAX_SUPPORT_TEXT, MAX_TITLE, MAX_MEMO, MAX_URL = 8, 300, 30, 4000, 500
DEFAULT_AMOUNT_TITLE = "받을 수 있는 금액"
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
    apply_url = str(data.get("apply_url") or "").strip()
    if apply_url and (not apply_url.startswith(("https://", "http://", "mailto:")) or len(apply_url) > MAX_URL or re.search(r"\s", apply_url)):
        problems.append("신청 사이트 주소는 https://(또는 http://, mailto:)로 시작하는 공백 없는 주소여야 해요.")
    memo = str(data.get("ai_memo") or "").strip()
    if len(memo) > MAX_MEMO:
        problems.append(f"AI 조사 메모는 {MAX_MEMO}자 이하로 써 주세요.")
    amount_title = str(data.get("amount_title") or "").strip()
    support = []
    for a in (data.get("support_lines") or []):
        if not isinstance(a, dict) or not (str(a.get("label") or "").strip() or str(a.get("text") or "").strip()):
            continue
        item = {"label": str(a.get("label") or "").strip(), "text": str(a.get("text") or "").strip()}
        when = sorted({w for w in (a.get("when") or []) if isinstance(w, int) and not isinstance(w, bool)})
        if when:                       # 표시 조건: 체크리스트 번호(1부터) 중 하나라도 체크했을 때만 이 줄을 보인다. 없으면 항상 보인다
            item["when"] = when
        support.append(item)
    if len(amount_title) > MAX_TITLE:
        problems.append(f"금액 안내 제목은 {MAX_TITLE}자 이하로 써 주세요.")
    if len(support) > MAX_SUPPORT:
        problems.append(f"지원 내용 항목은 {MAX_SUPPORT}개 이하로 써 주세요.")
    for i, a in enumerate(support, 1):
        if any(not 1 <= w <= (len(needs) or MAX_NEEDS) for w in a.get("when", [])):
            problems.append(f"지원 내용 항목 {i}: 표시 조건 번호가 자격 체크리스트 범위를 벗어났어요.")
        if not a["label"] or not a["text"]:
            problems.append(f"지원 내용 항목 {i}: 이름과 내용을 모두 써 주세요.")
        elif len(a["label"]) > MAX_LABEL or len(a["text"]) > MAX_SUPPORT_TEXT:
            problems.append(f"지원 내용 항목 {i}: 글이 너무 길어요 (이름 {MAX_LABEL}자, 내용 {MAX_SUPPORT_TEXT}자).")
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
    if not (benefit or how or needs or notes or amounts or support or amount_title or apply_url) and default_layout:
        problems.append("바꿀 내용을 하나 이상 써 주세요.")
    if problems:
        return None, problems
    return {"benefit_text": benefit, "how_to_apply": how, "needs_check": needs, "notes": notes, "amounts": amounts,
            "amount_title": amount_title, "support_lines": support, "ai_memo": memo, "source": source if amounts else {}, **({} if default_layout else {"layout": layout}),
            **({"apply_url": apply_url} if apply_url else {})}, []


def overrides(folder: Path | None = None) -> dict[str, dict]:
    """제도 -> 현재 구성 (입력자 파일을 모두 읽어 시각순으로 적용한다: set이 구성을 바꾸고, confirm이 AI 구성을 확인 처리하고, clear가 지운다). 파일이 바뀌면 다시 읽는다.
    구성에는 by·at(마지막 저장), ai(AI가 채운 구성인지), confirmed_by·confirmed_at(AI 구성의 확인)이 붙는다."""
    folder = folder or OVERRIDE_DIR
    files = sorted(folder.glob("*.jsonl")) if folder.is_dir() else []
    stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    hit = _cache.get(str(folder))
    if hit and hit[0] == stamp:
        return hit[1]
    events: list[dict] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("service_id") and e.get("action") in ("set", "clear", "confirm"):
                events.append(e)
    result: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: e.get("at", "")):
        sid = e["service_id"]
        if e["action"] == "set" and isinstance(e.get("data"), dict):
            result[sid] = {**e["data"], "by": e.get("by", ""), "at": e.get("at", ""), "ai": bool(e.get("ai"))}
        elif e["action"] == "clear":
            result.pop(sid, None)
        elif sid in result and result[sid].get("ai"):
            result[sid] = {**result[sid], "confirmed_by": e.get("by", ""), "confirmed_at": e.get("at", "")}
    _cache[str(folder)] = (stamp, result)
    return result


def pending_ai(ov: dict | None) -> bool:
    """AI가 채웠고 아직 사람이 확인하지 않은 구성인가."""
    return bool(ov and ov.get("ai") and not ov.get("confirmed_by"))


def _write(service_id: str, editor: str, action: str, data: dict | None, folder: Path | None, ai: bool = False) -> None:
    if not valid_editor(editor):
        raise ValueError("입력자 이름은 영문·숫자·_-만 쓸 수 있어요.")
    folder = folder or OVERRIDE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    event = {"service_id": service_id, "action": action, "data": data, "by": editor, "at": datetime.now(timezone.utc).isoformat(), **({"ai": True} if ai else {})}
    with (folder / f"{editor}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def save(service_id: str, editor: str, data: dict, folder: Path | None = None, *, ai: bool = False) -> None:
    """구성을 저장한다. ai=True는 AI가 채운 구성(사람이 확인하기 전에는 실제 답변에 안 나온다)."""
    clean, problems = validate(data)
    if clean is None:
        raise ValueError("; ".join(problems))
    _write(service_id, editor, "set", clean, folder, ai)


def confirm(service_id: str, editor: str, folder: Path | None = None) -> None:
    """AI가 채운 구성을 사람이 확인했다고 기록한다 (이때부터 실제 답변에 나온다). AI 구성이 아니면 오류."""
    if not pending_ai(overrides(folder).get(service_id)):
        raise ValueError("확인할 AI 구성이 없어요.")
    _write(service_id, editor, "confirm", None, folder)


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
    """실제 답변에 쓰는 구성. AI가 채우고 아직 확인되지 않은 구성은 돌려주지 않는다."""
    pending = getattr(_preview, "value", None)
    if pending is not None and pending[0] == service_id:
        return pending[1]
    ov = overrides().get(service_id) if service_id else None
    return None if pending_ai(ov) else ov


def find_any(service_id: str) -> dict | None:
    """관리 화면용: AI가 채운 미확인 구성도 돌려준다."""
    return overrides().get(service_id) if service_id else None


def apply(r: Recommendation) -> Recommendation:
    """저장된 구성이 있으면 카드에 쓰는 문구를 바꾼 사본을 돌려준다 (없으면 그대로). 바꾸는 것: 무엇을 받나요·신청방법·자격 체크리스트·신청 사이트 주소(apply_url)."""
    ov = find(r.service_id)
    if not ov:
        return r
    return replace(r, benefit=ov.get("benefit_text") or r.benefit, how_to_apply=ov.get("how_to_apply") or r.how_to_apply,
                   needs_check=list(ov.get("needs_check") or r.needs_check), apply_url=ov.get("apply_url") or r.apply_url)
