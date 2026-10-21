"""평가 질문 세트(eval/questions.jsonl)의 정답을 채우는 도구. 자세한 사용법: docs/LABELING.md

A의 파이프라인이 끝나기 전에도 쓸 수 있게, 보조금24 API에서 서비스 목록을 직접 받아 키워드로 찾아보고
**사람이 읽고 판단해서** 고른 `서비스ID`를 질문의 `relevant_service_ids`에 기록한다.
이 도구는 정답을 대신 정해 주지 않는다 (검색 결과를 그대로 정답으로 삼으면 평가가 순환하므로).

사용 (프로젝트 폴더에서 `python -m eval.label_helper <명령>`):
    python -m eval.label_helper fetch                    # 서비스 목록 내려받기 (data/raw/serviceList.json, 한 번만)
    python -m eval.label_helper fields                   # 응답에 어떤 필드가 있는지 확인
    python -m eval.label_helper search 월세              # 키워드로 찾기 (모든 단어 포함). 아래 옵션 참고
    python -m eval.label_helper search 월세 --region 서울  # 서울 제도 + 전국 제도만
    python -m eval.label_helper search 월세 --scope national   # 전국 단위(중앙부처·공공기관) 제도만
    python -m eval.label_helper show <서비스ID>           # 한 제도를 자세히 읽기 (--full: 줄이지 않고)
    python -m eval.label_helper label                    # 정답이 비어 있는 질문을 차례로 보며 채우기 (대화형)
    python -m eval.label_helper followup q01 --region 서울 # q01의 '지역을 알려 준 뒤' 질문을 새 문항으로 만들기
    python -m eval.label_helper set q05 ID1 ID2          # 질문 q05의 정답을 직접 지정
    python -m eval.label_helper none q32                 # 질문 q32를 '근거 없음'으로 확정
    python -m eval.label_helper status                   # 진행 상황
    python -m eval.label_helper check                    # 정답 id 검증 + 경고(지역·기한)

search 옵션: --any(하나라도 포함) --youth(청년 키워드만) --region 서울 --scope all|national|local
             --show-expired(기한이 지난 듯한 제도도 보기) --limit 20 --today 2026-10-19

정답을 정하는 규칙 (지역을 나중에 묻는 서비스 흐름 기준)
    - 실제 사용자는 처음에 지역 없이 묻고, 서비스가 추가 질문으로 지역을 받는다.
      그래서 **지역이 없는 질문**의 정답은 **전국 단위 제도**(중앙부처·공공기관)로 정한다. 지자체 제도는 지역을 알아야 안내할 수 있다.
    - 지역을 받은 뒤의 질문은 **별도 문항**으로 둔다 (`followup` 명령). 그 문항의 정답은 그 지역 제도(+ 전국 제도)다.
    - 신청기한이 지난 제도는 정답에서 뺀다 (기한은 자유 형식 문자열이라 '지난 듯함'으로만 경고한다).
    - 한 질문에 정답 5개 이내, 질문 하나만으로 좋은 답이 되는 제도만.

전제: .env에 GOV24_API_KEY(Decoding 키). A가 이미 data/raw/serviceList.json을 만들어 두었으면 그것을 그대로 쓴다.
질문을 고치기 전에 data/raw/label_backups/에 백업을 만든다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):      # Windows 콘솔에서 인코딩 오류로 멈추지 않게
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import config  # noqa: E402

CACHE_PATH = config.RAW_DIR / "serviceList.json"
QUESTIONS_PATH = config.EVAL_DIR / "questions.jsonl"
BACKUP_DIR = config.RAW_DIR / "label_backups"

# 응답 필드 이름 후보 (실제 응답에서 확인되는 첫 번째 이름을 쓴다). `fields` 명령으로 실제 필드를 볼 수 있다.
ALIASES = {
    "id": ["서비스ID", "서비스아이디", "svcId", "service_id"],
    "name": ["서비스명", "svcNm", "service_name"],
    "summary": ["서비스목적요약", "서비스목적", "서비스요약"],
    "target": ["지원대상"],
    "criteria": ["선정기준"],
    "content": ["지원내용"],
    "apply": ["신청방법"],
    "deadline": ["신청기한"],
    "agency": ["소관기관명", "소관기관", "부서명"],
    "agency_type": ["소관기관유형", "기관유형"],
    "field": ["서비스분야", "사용자구분"],
    "url": ["상세조회URL", "상세URL"],
}
REQUIRED = ("id", "name")
DETAIL_FIELDS = [("summary", "목적"), ("target", "지원대상"), ("criteria", "선정기준"), ("content", "지원내용"),
                 ("apply", "신청방법"), ("deadline", "신청기한"), ("agency", "소관기관"), ("field", "분야"), ("url", "링크")]
SHORT = 300          # 상세 보기에서 항목당 보여 줄 글자 수 (--full이면 전부)

# ---------------------------------------------------------------------------
# 지역 (소관기관으로 전국/지자체를 구분한다)
# ---------------------------------------------------------------------------
SIDO_FULL = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기", "강원도": "강원",
    "강원특별자치도": "강원", "충청북도": "충북", "충청남도": "충남", "전라북도": "전북", "전북특별자치도": "전북",
    "전라남도": "전남", "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주", "제주도": "제주",
}
SIDO_SHORT = set(SIDO_FULL.values())
_FULL_BY_LENGTH = sorted(SIDO_FULL, key=len, reverse=True)
_SHORT_BY_LENGTH = sorted(SIDO_SHORT, key=len, reverse=True)
_LOCAL_UNIT = re.compile(r"^[가-힣]{1,6}(시|군|구)(\s|$)")


def norm_region(value) -> str | None:
    """'서울'·'서울특별시'·'경기도' -> 짧은 이름. 알 수 없으면 None."""
    v = str(value or "").strip()
    if v in SIDO_FULL:
        return SIDO_FULL[v]
    if v in SIDO_SHORT:
        return v
    if v.endswith(("시", "도")) and v[:-1] in SIDO_SHORT:
        return v[:-1]
    return None


def service_scope(row: dict) -> tuple[str, str | None]:
    """(범위, 지역). 범위: 'national'(전국 단위) / 'local'(지자체·지역 기관). 지역은 짧은 이름, 알 수 없으면 None.

    소관기관명(없으면 기관유형)으로 추정하는 규칙이다: 기관명이 시·도 이름으로 시작하면 그 지역 제도,
    '○○시/군/구'처럼 시도 없이 시·군·구만 있으면 지역 미상의 지자체 제도, 나머지는 전국 단위로 본다.
    기관유형(소관기관유형)이 '중앙'이면 전국, '지방'이면 지자체로 본다. 추정이므로 `show`로 확인하세요."""
    agency = get(row, "agency")
    atype = get(row, "agency_type")
    if "중앙" in atype:
        return "national", None
    for full in _FULL_BY_LENGTH:
        if agency.startswith(full):
            return "local", SIDO_FULL[full]
    for short in _SHORT_BY_LENGTH:
        if agency.startswith(short) and len(agency) > len(short):     # 예: 경기주택도시공사, 서울시설공단
            return "local", short
    if "지방" in atype or "지자체" in atype or _LOCAL_UNIT.match(agency):
        return "local", None
    return "national", None


def scope_label(row: dict) -> str:
    scope, region = service_scope(row)
    return "전국" if scope == "national" else (region or "지역미상")


# ---------------------------------------------------------------------------
# 신청기한 (자유 형식 문자열이라 '지난 듯함'만 추정한다)
# ---------------------------------------------------------------------------
_FULL_DATE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
_PART_DATE = re.compile(r"(?<![\d:])(\d{1,2})\s*[.월]\s*(\d{1,2})(?![\d:])")
_OPEN_ENDED = ("상시", "연중", "소진", "수시")


def _mk_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def deadline_status(text: str, today: date | str | None = None) -> tuple[str, date | None]:
    """신청기한 문자열 -> ('expired' | 'open' | 'unknown', 마지막 날짜).

    - 'expired': 끝나는 날짜가 오늘보다 이전이거나, 날짜 없이 '마감'이라고만 적힌 경우 (추정)
    - 'open'   : 끝나는 날짜가 오늘 이후
    - 'unknown': 상시·연중·예산 소진 시까지·'접수기관 별 상이'·'공고' 등 판단할 수 없는 경우
    예) '2025.6.11.(수) 10:00 ~ 6. 24.(화)18:00' -> expired, '2026.10.12~2026.10.30' -> open(오늘이 10/19일 때)"""
    today_d = datetime.strptime(today, "%Y-%m-%d").date() if isinstance(today, str) else (today or date.today())
    t = text or ""
    if not t.strip():
        return "unknown", None
    if any(k in t for k in _OPEN_ENDED):
        return "unknown", None
    dates: list[date] = []
    last_year = None
    for m in _FULL_DATE.finditer(t):
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            dates.append(d)
            last_year = d.year
    if last_year is not None:                         # 연도 없이 '6. 24'처럼 적힌 끝 날짜는 앞선 연도를 이어 쓴다
        rest = _FULL_DATE.sub(" ", t)
        for m in _PART_DATE.finditer(rest):
            d = _mk_date(last_year, int(m.group(1)), int(m.group(2)))
            if d:
                if d < max(dates):                    # 연말~연초에 걸친 기간
                    d = _mk_date(last_year + 1, d.month, d.day) or d
                dates.append(d)
    if not dates:
        return ("expired", None) if "마감" in t else ("unknown", None)
    end = max(dates)
    return ("expired" if end < today_d else "open"), end


def deadline_tag(row: dict, today) -> str:
    status, _ = deadline_status(get(row, "deadline"), today)
    return {"expired": " [기한 지남?]", "open": " [진행중]"}.get(status, "")


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------
def get(row: dict, key: str) -> str:
    """별칭 후보 중 값이 있는 첫 번째 필드의 값을 문자열로."""
    for name in ALIASES[key]:
        if row.get(name) not in (None, ""):
            return str(row[name]).strip()
    return ""


def fetch_rows(key: str, per_page: int = 500, max_pages: int = 60) -> list[dict]:
    import requests

    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        resp = requests.get(f"{config.GOV24_API_BASE}/serviceList",
                            params={"serviceKey": key, "page": page, "perPage": per_page, "returnType": "JSON"}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("data", [])
        if not batch:
            break
        rows.extend(batch)
        print(f"  {len(rows)} / {data.get('totalCount', '?')}건")
        if len(rows) >= data.get("totalCount", 0):
            break
        page += 1
    return rows


def save_cache(rows: list[dict], path: Path | None = None) -> None:
    path = path or CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(rows, ensure_ascii=False))


def load_cache(path: Path | None = None) -> list[dict]:
    path = path or CACHE_PATH
    if not path.exists():
        raise SystemExit(f"{path} 가 없습니다. 먼저: python -m eval.label_helper fetch")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not rows or not get(rows[0], "id"):
        raise SystemExit("서비스ID 필드를 찾지 못했습니다. `fields` 명령으로 실제 필드 이름을 확인하고 ALIASES를 고치세요.")
    return rows


def index_by_id(rows: list[dict]) -> dict[str, dict]:
    return {get(r, "id"): r for r in rows if get(r, "id")}


# ---------------------------------------------------------------------------
# 검색 (키워드 일치. 우리 RAG 검색이 아니다 - 정답 후보를 눈으로 찾는 용도)
# ---------------------------------------------------------------------------
@dataclass
class View:
    """검색 결과를 어떻게 보여 줄지. 질문마다 `label`이 기본값을 정한다."""
    region: str | None = None          # 지정하면 그 지역 제도 + 전국 제도 (scope가 all일 때)
    scope: str = "all"                 # all | national | local
    hide_expired: bool = True
    youth: bool = False
    today: date = dc_field(default_factory=date.today)


@dataclass
class SearchResult:
    rows: list[dict]
    total: int                         # 키워드에 걸린 전체 건수
    hidden_scope: int = 0              # 지역·범위 때문에 숨긴 수
    hidden_expired: int = 0            # 기한이 지난 듯해서 숨긴 수


def _scope_ok(row: dict, view: View) -> bool:
    scope, region = service_scope(row)
    if view.scope == "national":
        return scope == "national"
    if view.scope == "local" and scope != "local":
        return False
    if view.region and scope == "local" and region not in (None, view.region):
        return False                   # 다른 지역 제도는 숨김 (지역 미상은 남겨서 사람이 확인)
    return True


def search(rows: list[dict], terms: list[str], any_term: bool = False, view: View | None = None) -> SearchResult:
    view = view or View(hide_expired=False)
    scored = []
    for r in rows:
        name, head, body = get(r, "name"), get(r, "summary") + " " + get(r, "target"), get(r, "criteria") + " " + get(r, "content")
        hay = f"{name} {head} {body}"
        hits = [t for t in terms if t in hay]
        if not hits or (not any_term and len(hits) < len(terms)):
            continue
        if view.youth and not any(k in hay for k in config.YOUTH_KEYWORDS):
            continue
        score = sum((3 if t in name else 0) + (2 if t in head else 0) + (1 if t in body else 0) for t in hits)
        scored.append((-score, name, r))
    scored.sort(key=lambda x: (x[0], x[1]))
    matched = [r for _, _, r in scored]
    result = SearchResult(rows=[], total=len(matched))
    for r in matched:
        if not _scope_ok(r, view):
            result.hidden_scope += 1
        elif view.hide_expired and deadline_status(get(r, "deadline"), view.today)[0] == "expired":
            result.hidden_expired += 1
        else:
            result.rows.append(r)
    return result


def short_line(r: dict, today=None) -> str:
    parts = [get(r, "agency"), get(r, "field"), (f"기한 {get(r, 'deadline')}" if get(r, "deadline") else "")]
    return f"{get(r, 'id')}  {get(r, 'name')}  | [{scope_label(r)}] " + " · ".join(p for p in parts if p) + deadline_tag(r, today)


def detail_text(r: dict, full: bool = False, today=None) -> str:
    status, end = deadline_status(get(r, "deadline"), today)
    status_text = {"expired": "기한이 지난 것으로 추정", "open": "진행 중으로 보임", "unknown": "판단 불가(상시·공고 등)"}[status]
    lines = [f"■ {get(r, 'name')}   (서비스ID: {get(r, 'id')})",
             f"  [범위] {scope_label(r)}   [기한 판정] {status_text}" + (f" (끝 {end})" if end else "")]
    for key, label in DETAIL_FIELDS:
        value = get(r, key)
        if value:
            lines.append(f"  [{label}] {value if full or len(value) <= SHORT else value[:SHORT] + ' …(--full로 전체 보기)'}")
    return "\n".join(lines)


def describe_view(view: View) -> str:
    if view.scope == "national":
        where = "전국 단위 제도만"
    elif view.scope == "local":
        where = (f"{view.region} 지자체 제도만" if view.region else "지자체 제도만")
    else:
        where = (f"{view.region} 제도 + 전국 제도" if view.region else "지역 구분 없이 전부")
    return f"{where} / 기한 지난 듯한 제도 {'숨김' if view.hide_expired else '표시'} / 청년 키워드 {'켬' if view.youth else '끔'}"


# ---------------------------------------------------------------------------
# 질문 파일
# ---------------------------------------------------------------------------
def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_questions(path: Path | None = None) -> list[dict]:
    path = path or QUESTIONS_PATH
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_questions(questions: list[dict], path: Path | None = None, backup_dir: Path | None = None) -> None:
    path, backup_dir = path or QUESTIONS_PATH, backup_dir or BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        (backup_dir / f"questions_{datetime.now():%Y%m%d-%H%M%S-%f}.jsonl").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    atomic_write(path, "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions))


def state(q: dict) -> str:
    """labeled(정답 있음) / no_answer(근거 없음, 확인 완료) / no_answer_check(근거 없음이지만 todo 남음) / todo."""
    if q.get("relevant_service_ids"):
        return "labeled"
    if q.get("expect_no_answer"):
        return "no_answer_check" if q.get("todo") else "no_answer"
    return "todo"


PROVENANCE_KEYS = ("label_source", "label_note", "label_confidence", "reviewed")


def set_provenance(q: dict, source: str | None = None, note: str | None = None, confidence: str | None = None) -> None:
    """누가 채웠는지 기록. source가 사람이 아닌 값(예: claude)이면 사람이 검수하기 전까지 reviewed=False."""
    if not source:
        for k in PROVENANCE_KEYS:
            q.pop(k, None)
        return
    q["label_source"] = source
    q["reviewed"] = source == "human"
    for key, value in (("label_note", note), ("label_confidence", confidence)):
        if value:
            q[key] = value
        else:
            q.pop(key, None)


def set_labels(q: dict, ids: list[str], by_id: dict[str, dict], source: str | None = None, note: str | None = None,
               confidence: str | None = None, todo: str | None = None) -> None:
    q["relevant_service_ids"] = list(dict.fromkeys(ids))
    q["relevant_service_names"] = [get(by_id[i], "name") for i in q["relevant_service_ids"]]
    q["expect_no_answer"] = False
    q.pop("todo", None)
    if todo:
        q["todo"] = todo
    set_provenance(q, source, note, confidence)


def set_no_answer(q: dict, source: str | None = None, note: str | None = None, confidence: str | None = None,
                  todo: str | None = None) -> None:
    q["relevant_service_ids"] = []
    q.pop("relevant_service_names", None)
    q["expect_no_answer"] = True
    q.pop("todo", None)
    if todo:
        q["todo"] = todo
    set_provenance(q, source, note, confidence)


def needs_review(q: dict) -> bool:
    """사람이 아닌 쪽(예: Claude)이 채운 뒤 아직 검수하지 않은 문항."""
    return bool(q.get("label_source")) and not q.get("reviewed", False)


def apply_decisions(questions: list[dict], decisions: dict[str, dict], by_id: dict[str, dict], source: str = "claude") -> list[str]:
    """{qid: {ids, note, confidence, [todo], [no_answer]}} 를 한꺼번에 반영. 데이터에 없는 id가 있으면 아무것도 바꾸지 않고 중단한다."""
    for qid, d in decisions.items():
        bad = unknown_ids(d.get("ids", []), by_id)
        if bad:
            raise SystemExit(f"{qid}: 데이터에 없는 서비스ID {bad}")
    done = []
    for qid, d in decisions.items():
        q = find_question(questions, qid)
        kw = dict(source=source, note=d.get("note"), confidence=d.get("confidence"), todo=d.get("todo"))
        if d.get("ids"):
            set_labels(q, d["ids"], by_id, **kw)
        elif d.get("no_answer"):
            set_no_answer(q, **kw)
        else:
            q["relevant_service_ids"] = []
            q.pop("relevant_service_names", None)
            q["expect_no_answer"] = False
            if d.get("todo"):
                q["todo"] = d["todo"]
            set_provenance(q, source, d.get("note"), d.get("confidence"))
        done.append(qid)
    return done


def find_question(questions: list[dict], qid: str) -> dict:
    for q in questions:
        if q["id"] == qid:
            return q
    raise SystemExit(f"질문 {qid!r}를 찾을 수 없습니다. 있는 id: {', '.join(q['id'] for q in questions[:5])} ...")


def unknown_ids(ids: list[str], by_id: dict[str, dict]) -> list[str]:
    return [i for i in ids if i not in by_id]


def question_region(q: dict) -> str | None:
    return norm_region((q.get("filters") or {}).get("region"))


def default_view(q: dict, today: date | None = None) -> View:
    """질문에 지역이 있으면 그 지역 제도 + 전국 제도, 없으면 전국 단위 제도만 (지역은 서비스가 추가 질문으로 받는 정보라서)."""
    region = question_region(q)
    return View(region=region, scope="all" if region else "national", hide_expired=True, youth=False, today=today or date.today())


def label_warnings(q: dict, row: dict, today=None) -> list[str]:
    """정답으로 고른 제도가 질문의 지역·기한과 안 맞을 때의 경고."""
    msgs = []
    scope, region = service_scope(row)
    qreg = question_region(q)
    if scope == "local":
        if not qreg:
            msgs.append(f"지역이 없는 질문인데 지자체 제도입니다 ({region or '지역미상'}). 지역을 받은 뒤 질문(followup)의 정답으로 두세요")
        elif region and region != qreg:
            msgs.append(f"질문의 지역({qreg})과 다른 지역({region}) 제도입니다")
    if deadline_status(get(row, "deadline"), today)[0] == "expired":
        msgs.append("신청기한이 지난 것으로 보입니다")
    return msgs


def make_followup(questions: list[dict], qid: str, region: str, text: str | None = None, age: int | None = None) -> dict:
    """q의 '지역을 알려 준 뒤' 버전을 새 문항으로 만들어 원래 질문 바로 뒤에 넣는다. 정답은 비워 둔다."""
    base = find_question(questions, qid)
    short = norm_region(region)
    if not short:
        raise SystemExit(f"알 수 없는 지역: {region!r} (예: 서울, 경기, 부산)")
    nums = [int(m.group(1)) for q in questions if (m := re.fullmatch(r"q(\d+)", q["id"]))]
    new_id = f"q{max(nums, default=0) + 1:02d}"
    filters = dict(base.get("filters") or {})
    filters["region"] = short
    if age is not None:
        filters["age"] = age
    new = {"id": new_id, "category": "지역 후속 질문", "topic": base.get("topic", ""),
           "question": text or f"{short}에 살아요. {base['question']}",
           "relevant_service_ids": [],
           "expected_answer": f"{short} 지역 제도와 전국 제도를 함께 안내한다. (원 질문 {qid}: {base.get('expected_answer', '')})",
           "expect_no_answer": False, "filters": filters, "follow_up_of": qid,
           "todo": f"{short} 지역 제도 + 전국 제도 중 핵심 후보를 데이터에서 읽고 정답 지정"}
    questions.insert(questions.index(base) + 1, new)
    return new


# ---------------------------------------------------------------------------
# 대화형 채우기
# ---------------------------------------------------------------------------
HELP = """  명령:
    (글자)         그 키워드로 검색 (띄어쓰기로 여러 단어 = 모두 포함). 결과가 없으면 하나라도 포함으로 다시 찾음
    1 3            방금 결과의 1번·3번을 정답 후보에 추가/해제
    d 2            2번 제도를 자세히 읽기 (D 2 → 전체)
    more           다음 결과 보기
    id:<서비스ID>   서비스ID를 직접 추가
    chosen / clear 고른 후보 보기 / 비우기
    region 서울     이 질문에서 서울 제도 + 전국 제도로 보기 (region off: 해제)
    scope national 전국 단위만 / local 지자체만 / all 전부
    expired on|off 기한이 지난 듯한 제도 보기 / 숨기기
    youth          청년 키워드 필터 켜기/끄기
    view           현재 보기 설정
    ok             고른 후보를 이 질문의 정답으로 저장하고 다음 질문으로 (지역·기한이 안 맞으면 경고 후 확인)
    none           찾아봤지만 정답이 없다 → '근거 없음'으로 확정
    keep           (이미 근거 없음인 질문) 데이터를 확인했고 그대로 둔다
    skip           건너뛰기   quit  종료(여기까지 저장됨)"""


def label_session(questions: list[dict], rows: list[dict], only: list[str] | None = None, include_done: bool = False,
                  input_fn=input, print_fn=print, save_fn=None, today: date | None = None) -> int:
    """질문을 차례로 보며 정답을 채운다. 저장한 질문 수를 돌려준다. input_fn/print_fn은 테스트용으로 바꿀 수 있다."""
    by_id = index_by_id(rows)
    save = save_fn or (lambda qs: save_questions(qs))
    today = today or date.today()
    targets = [q for q in questions if (not only or q["id"] in only) and (include_done or only or state(q) in ("todo", "no_answer_check"))]
    if not targets:
        print_fn("채울 질문이 없습니다. (이미 정답이 있는 질문까지 보려면 --all)")
        return 0
    saved = 0
    print_fn(f"질문 {len(targets)}개를 차례로 봅니다. 'help'로 명령 보기, 'quit'로 종료.\n")
    for n, q in enumerate(targets, 1):
        chosen: list[str] = list(q.get("relevant_service_ids", []))
        results: list[dict] = []
        view = default_view(q, today)
        offset = 0
        print_fn("=" * 72)
        print_fn(f"[{n}/{len(targets)}] {q['id']}  ({q.get('category', '')} / {q.get('topic', '')})  상태: {state(q)}")
        print_fn(f"질문: {q['question']}")
        if q.get("filters"):
            print_fn(f"조건: {q['filters']}")
        print_fn(f"기대 답변: {q.get('expected_answer', '')}")
        if q.get("todo"):
            print_fn(f"메모: {q['todo']}")
        if view.region:
            print_fn(f"[지역 {view.region}] 이 지역 제도 + 전국 제도를 보여 줍니다.")
        else:
            print_fn("[지역 없음] 서비스는 지역을 추가 질문으로 받으므로, 이 질문의 정답은 전국 단위 제도로 정합니다. "
                     "지자체 제도는 `followup`으로 지역 질문을 만들어 채우세요. (지자체 제도도 보려면 `scope all`)")
        while True:
            cmd = input_fn("> ").strip()
            low = cmd.lower()
            words = low.split()
            if low in ("q", "quit", "exit"):
                return saved
            if low in ("", "skip", "s"):
                break
            if low == "help":
                print_fn(HELP)
            elif low == "view":
                print_fn("  " + describe_view(view))
            elif low == "youth":
                view.youth = not view.youth
                print_fn(f"  청년 키워드 필터: {'켜짐' if view.youth else '꺼짐'}")
            elif words[0] == "region" and len(words) == 2:
                if words[1] == "off":
                    view.region = None
                    print_fn("  지역 필터 해제")
                else:
                    reg = norm_region(cmd.split()[1])
                    if not reg:
                        print_fn(f"  알 수 없는 지역: {cmd.split()[1]} (예: 서울, 경기, 부산)")
                    else:
                        view.region, view.scope = reg, "all"
                        print_fn(f"  {reg} 제도 + 전국 제도로 봅니다.")
            elif words[0] == "scope" and len(words) == 2 and words[1] in ("all", "national", "local"):
                view.scope = words[1]
                print_fn("  " + describe_view(view))
            elif words[0] == "expired" and len(words) == 2 and words[1] in ("on", "off"):
                view.hide_expired = words[1] == "off"          # expired on = 지난 제도도 보기
                print_fn("  " + describe_view(view))
            elif low == "chosen":
                print_fn("  고른 후보: " + (", ".join(f"{i}({get(by_id[i], 'name') if i in by_id else '?'})" for i in chosen) or "(없음)"))
            elif low == "clear":
                chosen.clear()
                print_fn("  후보를 비웠습니다.")
            elif low == "more":
                offset += 10
                print_results(results, offset, print_fn, today=today)
            elif low in ("ok", "save"):
                if not chosen:
                    print_fn("  고른 후보가 없습니다. 번호를 고르거나(id:..), 정답이 없다면 none.")
                    continue
                bad = unknown_ids(chosen, by_id)
                if bad:
                    print_fn(f"  데이터에 없는 서비스ID: {bad}. clear 후 다시 고르세요.")
                    continue
                warns = [(i, w) for i in chosen for w in label_warnings(q, by_id[i], today)]
                if warns:
                    for i, w in warns:
                        print_fn(f"  ! {get(by_id[i], 'name')}: {w}")
                    if input_fn("  그래도 저장할까요? (y/N) ").strip().lower() != "y":
                        print_fn("  저장하지 않았습니다. clear 후 다시 고르거나 id를 바꾸세요.")
                        continue
                set_labels(q, chosen, by_id)
                save(questions)
                saved += 1
                print_fn(f"  저장: {q['id']} ← {q['relevant_service_ids']}")
                break
            elif low == "none":
                if input_fn("  정말 이 질문에 맞는 제도가 데이터에 없나요? (y/N) ").strip().lower() == "y":
                    set_no_answer(q)
                    save(questions)
                    saved += 1
                    print_fn(f"  저장: {q['id']} = 근거 없음")
                    break
            elif low == "keep":
                if not q.get("expect_no_answer"):
                    print_fn("  이 질문은 근거 없음이 아닙니다. 정답을 고르거나 none을 쓰세요.")
                    continue
                q.pop("todo", None)
                save(questions)
                saved += 1
                print_fn(f"  확인 완료로 저장: {q['id']}")
                break
            elif low.startswith("id:"):
                sid = cmd[3:].strip()
                if sid not in by_id:
                    print_fn(f"  데이터에 없는 서비스ID: {sid}")
                else:
                    if sid not in chosen:
                        chosen.append(sid)
                    print_fn(f"  추가: {get(by_id[sid], 'name')}")
                    for w in label_warnings(q, by_id[sid], today):
                        print_fn(f"  ! 주의: {w}")
            elif words[0] == "d" and len(words) == 2 and words[1].isdigit():
                idx = int(words[1])
                if 1 <= idx <= len(results):
                    print_fn(detail_text(results[idx - 1], full=(cmd.split()[0] == "D"), today=today))
                else:
                    print_fn("  그 번호의 결과가 없습니다.")
            elif all(tok.isdigit() for tok in words):
                for tok in words:
                    idx = int(tok)
                    if not 1 <= idx <= len(results):
                        print_fn(f"  {idx}번 결과가 없습니다.")
                        continue
                    sid = get(results[idx - 1], "id")
                    if sid in chosen:
                        chosen.remove(sid)
                        print_fn(f"  해제: {get(results[idx - 1], 'name')}")
                    else:
                        chosen.append(sid)
                        print_fn(f"  추가: {get(results[idx - 1], 'name')}")
                        for w in label_warnings(q, results[idx - 1], today):
                            print_fn(f"  ! 주의: {w}")
            else:
                terms = cmd.split()
                found = search(rows, terms, any_term=False, view=view)
                if not found.rows and found.total == 0:
                    found = search(rows, terms, any_term=True, view=view)
                    if found.total:
                        print_fn("  (모든 단어를 포함하는 제도가 없어 '하나라도 포함'으로 다시 찾았습니다)")
                results, offset = found.rows, 0
                print_fn(f"  {len(results)}건 표시" + _hidden_note(found))
                print_results(results, 0, print_fn, today=today)
    return saved


def _hidden_note(found: SearchResult) -> str:
    notes = []
    if found.hidden_scope:
        notes.append(f"지역·범위 때문에 {found.hidden_scope}건")
    if found.hidden_expired:
        notes.append(f"기한이 지난 듯해서 {found.hidden_expired}건")
    return f"  (숨김: {', '.join(notes)})" if notes else ""


def print_results(results: list[dict], offset: int, print_fn=print, page: int = 10, today=None) -> None:
    for i, r in enumerate(results[offset:offset + page], start=offset + 1):
        print_fn(f" {i:>2}. {short_line(r, today)}")
    if offset + page < len(results):
        print_fn(f"     … {len(results) - offset - page}건 더 (more)")


# ---------------------------------------------------------------------------
# 명령줄
# ---------------------------------------------------------------------------
def cmd_fetch(args) -> None:
    key = args.key or config.GOV24_API_KEY
    if not key:
        raise SystemExit(".env에 GOV24_API_KEY(Decoding 키)를 넣거나 --key로 전달하세요.")
    if CACHE_PATH.exists() and not args.force:
        raise SystemExit(f"{CACHE_PATH} 가 이미 있습니다 (A의 수집 결과일 수 있음). 다시 받으려면 --force.")
    print("보조금24 서비스 목록을 내려받는 중...")
    try:
        rows = fetch_rows(key)
    except Exception as e:  # 네트워크·인증 오류 안내
        raise SystemExit(f"내려받기 실패: {e}\n인증키가 Decoding 키인지, 활용신청이 승인됐는지 확인하세요.")
    save_cache(rows)
    print(f"저장: {CACHE_PATH}  ({len(rows)}건)")
    cmd_fields(args, rows)


def cmd_fields(args, rows: list[dict] | None = None) -> None:
    rows = rows or load_cache()
    print("응답 필드(첫 번째 서비스 기준):")
    for k, v in rows[0].items():
        print(f"  {k}: {str(v)[:50]!r}")
    missing = [key for key, names in ALIASES.items() if not any(n in rows[0] for n in names)]
    required_missing = [k for k in missing if k in REQUIRED]
    optional_missing = [k for k in missing if k not in REQUIRED]
    if required_missing:
        print(f"\n! 꼭 필요한데 찾지 못한 항목: {required_missing}  → ALIASES를 실제 필드 이름에 맞게 고치세요.")
    if optional_missing:
        print(f"\n(선택 항목 중 찾지 못한 것: {optional_missing}. 없어도 동작하지만 `deadline`이 없으면 기한 경고가, "
              f"`agency`가 없으면 전국/지역 구분이 부정확해집니다)")
    for r in rows[:3]:
        print(f"  예) {get(r, 'name')[:24]} → 범위 {scope_label(r)}, 기한 판정 {deadline_status(get(r, 'deadline'))[0]}")


def _today_arg(value: str | None) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else date.today()


def cmd_search(args) -> None:
    region = norm_region(args.region) if args.region else None
    if args.region and not region:
        raise SystemExit(f"알 수 없는 지역: {args.region!r} (예: 서울, 경기, 부산)")
    today = _today_arg(args.today)
    view = View(region=region, scope=args.scope, hide_expired=not args.show_expired, youth=args.youth, today=today)
    found = search(load_cache(), args.terms, args.any, view)
    print(f"보기: {describe_view(view)}")
    print(f"{len(found.rows)}건 표시 (키워드에 걸린 전체 {found.total}건)" + _hidden_note(found))
    print_results(found.rows[:args.limit], 0, page=args.limit, today=today)


def cmd_show(args) -> None:
    by_id = index_by_id(load_cache())
    if args.service_id not in by_id:
        raise SystemExit(f"서비스ID {args.service_id!r} 가 데이터에 없습니다.")
    print(detail_text(by_id[args.service_id], full=args.full, today=_today_arg(args.today)))


def cmd_status(args) -> None:
    qs = load_questions()
    counts: dict[str, int] = {}
    for q in qs:
        counts[state(q)] = counts.get(state(q), 0) + 1
    print(f"질문 {len(qs)}개: 정답 지정 {counts.get('labeled', 0)} / 근거 없음 확정 {counts.get('no_answer', 0)} / "
          f"근거 없음(확인 필요) {counts.get('no_answer_check', 0)} / 정답 미지정 {counts.get('todo', 0)}")
    pending = [q for q in qs if needs_review(q)]
    print(f"사람 검수 대기 {len(pending)}개 (Claude 등이 채웠고 아직 확인 전)" + (": " + ", ".join(q["id"] for q in pending) if pending else ""))
    for q in qs:
        if state(q) in ("todo", "no_answer_check"):
            region = question_region(q)
            print(f"  [{state(q)}] {q['id']} {'(' + region + ') ' if region else ''}{q['question'][:50]}")


def cmd_label(args) -> None:
    only = args.only.split(",") if args.only else None
    n = label_session(load_questions(), load_cache(), only=only, include_done=args.all, today=_today_arg(args.today))
    print(f"\n{n}개 질문을 저장했습니다. 확인: python -m eval.label_helper status / check")


def cmd_followup(args) -> None:
    qs = load_questions()
    new = make_followup(qs, args.qid, args.region, args.text, args.age)
    save_questions(qs)
    print(f"새 문항 {new['id']} 를 {args.qid} 뒤에 추가했습니다 (지역 {new['filters']['region']}).")
    print(f"  질문: {new['question']}")
    print(f"  정답 채우기: python -m eval.label_helper label --only {new['id']}")


def cmd_set(args) -> None:
    rows = load_cache()
    by_id = index_by_id(rows)
    bad = unknown_ids(args.ids, by_id)
    if bad:
        raise SystemExit(f"데이터에 없는 서비스ID: {bad}")
    qs = load_questions()
    q = find_question(qs, args.qid)
    for i in args.ids:
        for w in label_warnings(q, by_id[i], _today_arg(args.today)):
            print(f"! 주의 {get(by_id[i], 'name')}: {w}")
    set_labels(q, args.ids, by_id, source=args.source, note=args.note, confidence=args.confidence)
    save_questions(qs)
    print(f"{args.qid} ← {[get(by_id[i], 'name') for i in args.ids]}")


def drop_question(questions: list[dict], qid: str) -> dict:
    """질문 하나를 세트에서 뺀다. 이 질문에서 만든 후속 문항(follow_up_of)이 있으면 먼저 정리하도록 중단한다."""
    q = find_question(questions, qid)
    children = [x["id"] for x in questions if x.get("follow_up_of") == qid]
    if children:
        raise SystemExit(f"{qid}에서 만든 후속 문항이 있습니다: {', '.join(children)}. 먼저 그 문항을 drop 하세요")
    questions.remove(q)
    return q


def cmd_drop(args) -> None:
    qs = load_questions()
    q = drop_question(qs, args.qid)
    save_questions(qs)   # 저장 전 백업은 save_questions가 data/raw/label_backups/ 에 만든다
    print(f"{args.qid} 를 질문 세트에서 뺐습니다: {q['question'][:40]} (남은 {len(qs)}개). id는 다시 쓰이지 않으니 번호가 비어도 괜찮습니다")


def cmd_none(args) -> None:
    qs = load_questions()
    set_no_answer(find_question(qs, args.qid), source=args.source, note=args.note, confidence=args.confidence)
    save_questions(qs)
    print(f"{args.qid} = 근거 없음으로 저장")


def cmd_apply(args) -> None:
    decisions = json.loads(Path(args.file).read_text(encoding="utf-8"))
    qs = load_questions()
    done = apply_decisions(qs, decisions, index_by_id(load_cache()), source=args.source)
    save_questions(qs)
    print(f"{len(done)}개 질문에 반영했습니다 (출처 {args.source}, 사람 검수 전). 확인: python -m eval.label_helper review")


def cmd_review(args) -> None:
    qs = load_questions()
    if args.ok or args.all_ok:
        targets = [q for q in qs if needs_review(q)] if args.all_ok else [find_question(qs, i) for i in args.ok.split(",")]
        for q in targets:
            q["reviewed"] = True
        save_questions(qs)
        print(f"{len(targets)}개 질문을 검수 완료로 표시했습니다: {', '.join(q['id'] for q in targets)}")
        return
    pending = [q for q in qs if needs_review(q)]
    if args.only:
        wanted = set(args.only.split(","))
        pending = [q for q in qs if q["id"] in wanted]
    print(f"검수 대기 {len(pending)}개  (맞으면: review --ok q01,q02 / 고치려면: set·none 으로 직접 지정하면 사람이 채운 것으로 바뀜)\n")
    for q in pending:
        print(f"[{q['id']}] {q['question']}")
        if q.get("filters"):
            print(f"  조건: {q['filters']}")
        names = q.get("relevant_service_names") or []
        for i, sid in enumerate(q.get("relevant_service_ids", [])):
            print(f"  - {sid} {names[i] if i < len(names) else ''}")
        if not q.get("relevant_service_ids"):
            print("  (정답 없음: " + ("근거 없음 기대" if q.get("expect_no_answer") else "전국 단위 정답 없음") + ")")
        print(f"  신뢰도 {q.get('label_confidence', '-')} | {q.get('label_note', '')}")
        if q.get("todo"):
            print(f"  ! 사람 확인: {q['todo']}")
        print()


def cmd_check(args) -> None:
    by_id = index_by_id(load_cache())
    today = _today_arg(args.today)
    problems = warnings = 0
    for q in load_questions():
        ids = q.get("relevant_service_ids", [])
        names = q.get("relevant_service_names", [])
        for i, sid in enumerate(ids):
            if sid not in by_id:
                print(f"! {q['id']}: 서비스ID {sid!r} 가 데이터에 없음")
                problems += 1
                continue
            if names and i < len(names) and names[i] != get(by_id[sid], "name"):
                print(f"! {q['id']}: {sid} 의 이름이 다름 (기록 {names[i]!r} / 데이터 {get(by_id[sid], 'name')!r})")
                problems += 1
            for w in label_warnings(q, by_id[sid], today):
                print(f"~ 경고 {q['id']}: {get(by_id[sid], 'name')} - {w}")
                warnings += 1
        if q.get("expect_no_answer") and ids:
            print(f"! {q['id']}: 근거 없음인데 정답이 있음")
            problems += 1
        if len(ids) > 5:
            print(f"~ 경고 {q['id']}: 정답이 {len(ids)}개입니다. Recall@5가 구조적으로 낮아집니다 (5개 이내 권장)")
            warnings += 1
    print(("문제 없음" if not problems else f"문제 {problems}건") + (f", 경고 {warnings}건" if warnings else ""))
    raise SystemExit(1 if problems else 0)


def _add_provenance_args(p) -> None:
    p.add_argument("--source", help="채운 주체 (예: claude). 없으면 사람이 직접 채운 것으로 보고 출처 기록을 남기지 않음")
    p.add_argument("--note", help="판단 근거 한 줄")
    p.add_argument("--confidence", help="신뢰도 (높음/중간/낮음)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m eval.label_helper", description="평가 질문 세트의 정답(relevant_service_ids)을 채우는 도구. 사용법: docs/LABELING.md")
    sub = ap.add_subparsers(dest="cmd", required=True)
    today_help = "기준일 YYYY-MM-DD (기한 판정용, 기본은 오늘)"
    p = sub.add_parser("fetch", help="서비스 목록 내려받기")
    p.add_argument("--key")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_fetch)
    sub.add_parser("fields", help="응답 필드 확인").set_defaults(fn=cmd_fields)
    p = sub.add_parser("search", help="키워드 검색")
    p.add_argument("terms", nargs="+")
    p.add_argument("--any", action="store_true", help="하나라도 포함")
    p.add_argument("--youth", action="store_true", help="청년 키워드가 있는 제도만")
    p.add_argument("--region", help="이 지역 제도 + 전국 제도 (예: 서울)")
    p.add_argument("--scope", choices=["all", "national", "local"], default="all", help="전국 단위만 / 지자체만")
    p.add_argument("--show-expired", action="store_true", help="기한이 지난 듯한 제도도 보기")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--today", help=today_help)
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("show", help="한 제도 자세히")
    p.add_argument("service_id")
    p.add_argument("--full", action="store_true")
    p.add_argument("--today", help=today_help)
    p.set_defaults(fn=cmd_show)
    sub.add_parser("status", help="진행 상황").set_defaults(fn=cmd_status)
    p = sub.add_parser("label", help="대화형으로 채우기")
    p.add_argument("--only", help="쉼표로 구분한 질문 id (예: q05,q06)")
    p.add_argument("--all", action="store_true", help="이미 채운 질문도 다시 보기")
    p.add_argument("--today", help=today_help)
    p.set_defaults(fn=cmd_label)
    p = sub.add_parser("followup", help="지역을 알려 준 뒤의 후속 질문 문항 만들기")
    p.add_argument("qid")
    p.add_argument("--region", required=True)
    p.add_argument("--text", help="새 질문 문장 (기본: '<지역>에 살아요. <원 질문>')")
    p.add_argument("--age", type=int, help="조건에 나이도 넣기")
    p.set_defaults(fn=cmd_followup)
    p = sub.add_parser("set", help="정답 직접 지정")
    p.add_argument("qid")
    p.add_argument("ids", nargs="+")
    _add_provenance_args(p)
    p.add_argument("--today", help=today_help)
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser("none", help="근거 없음으로 확정")
    p.add_argument("qid")
    _add_provenance_args(p)
    p.set_defaults(fn=cmd_none)
    p = sub.add_parser("drop", help="질문을 세트에서 삭제 (폐기)")
    p.add_argument("qid")
    p.set_defaults(fn=cmd_drop)
    p = sub.add_parser("apply", help="결정 JSON({qid: {ids, note, confidence, todo}})을 한꺼번에 반영 (사람 검수 전으로 표시)")
    p.add_argument("file")
    p.add_argument("--source", default="claude", help="채운 주체 (기본 claude)")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("review", help="사람 검수: 목록 보기 / --ok q01,q02 로 검수 완료 표시")
    p.add_argument("--only", help="쉼표로 구분한 질문 id만 보기")
    p.add_argument("--ok", help="검수 완료로 표시할 질문 id (쉼표 구분)")
    p.add_argument("--all-ok", action="store_true", help="검수 대기 전부를 검수 완료로 표시")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("check", help="정답 id 검증")
    p.add_argument("--today", help=today_help)
    p.set_defaults(fn=cmd_check)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
