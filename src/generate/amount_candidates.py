"""예상 수령액 규칙을 만들 만한 제도를 DB에서 찾아 순위를 매긴다 (읽기 전용, LLM·네트워크 없음).

사람이 service_id를 일일이 고르지 않도록, 수집한 제도의 지원내용 원문을 훑어 금액이 있고 사용자 조건에 따라 달라지는 제도를 올려 준다.
출력은 후보 목록일 뿐이고 규칙 값은 만들지 않는다 (규칙은 공식 자료로 사람이 정하고 `amount_rules.json`에 넣는다).

    python -m src.generate.amount_candidates --top 30
    python -m src.generate.amount_candidates --top 100 --json data/a_validation/amount_candidates.json

판단 기준
- 금액: 지원내용에서 '원' 금액·비율(%)을 찾는다 (선정기준의 소득 %는 세지 않는다). 금액 문장은 원문에서 그대로 잘라 보여 준다.
- 구조: 구간형(조건별로 금액이 다름) > 정률+한도 > 정액 > 대출형(한도). 금액이 없으면(현물·서비스) 후보가 아니다.
- 입력 추정: 원문에서 나이·소득·재산·가구원·자녀·장애 언급을 찾아, 계산에 어떤 입력이 필요할지 추정한다.
- 수요: 검수 기록(`reviews/events`)에서 '좋음·부분'으로 판정된 횟수 (자주 추천되는 제도를 먼저).
- 이미 규칙이 있는 제도는 표시하고 순위에서 뒤로 보낸다."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from src.common import config
from src.generate import amount_rules

KINDS = ("구간형", "정률+한도", "정액", "대출형", "할인형")
BASE_SCORE = {"구간형": 50, "정률+한도": 35, "정액": 40, "대출형": 15, "할인형": 10}

_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:억|천만|백만|만|천)?\s*원")
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
_PERIOD_RE = re.compile(r"\d+\s*(?:개월|회|년|일)|매월|월\s*\d|연\s*\d|1인당|가구당|분기")
_CONDITION_RE = re.compile(r"이하|이상|미만|초과|명|인\s*가구|자녀|구간|유형|등급|단계|소득|세대|연령|\d+\s*세")
_CAP_RE = re.compile(r"최대|한도|이내|상한")
_COST_RE = re.compile(r"비용|이자|환급|보증료|수수료|등록금|임차료|월세|지원")
_LOAN_RE = re.compile(r"대출|융자|보증금|이차보전")
_DISCOUNT_NAME_RE = re.compile(r"이용료|이용요금|요금|관람료|입장|주차|감면|할인|면제")        # 시설 이용료 할인은 '받는 돈'을 계산할 일이 거의 없다
_SUPPORT_WORD_RE = re.compile(r"지원|환급|감면|할인|지급|보증|대출|융자|이자|부담|비용|요금|료|보조|수당|장학|바우처")
# 계산에 필요한 입력 단서 (입력 이름, 찾을 말)
_INPUT_HINTS = (
    ("나이", re.compile(r"만\s*\d+\s*세|청년|노인|어르신|\d+\s*세\s*(?:이상|이하|미만)")),
    ("소득", re.compile(r"중위소득|소득\s*(?:인정액|기준|\d)|월\s*소득|연\s*소득|소득\s*(?:하위|상위)")),
    ("재산", re.compile(r"재산|자산")),
    ("가구원 수", re.compile(r"\d\s*인\s*가구|가구원|가구\s*규모|세대원")),
    ("자녀 수", re.compile(r"자녀|출산|아동|영유아|다자녀")),
    ("장애", re.compile(r"장애")),
)
# 소관기관이 지자체인 제도는 사는 곳이 맞는 사람에게만 해당해서 순위를 낮춘다 (전국 제도 규칙을 먼저 만든다)
_LOCAL_RE = re.compile(r"^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청|충북|충남|전라|전북|전남|경상|경북|경남|제주)|시설관리공단|시설공단|문화재단|재단법인|구청|시청|군청")
LOCAL_PENALTY = 25
MAX_SENTENCES, MAX_SENTENCE_LEN = 4, 160


def _support_percents(line: str, context: str = "") -> list[str]:
    """줄에서 지원 비율(%)만: '중위소득 60% 이하'처럼 소득·재산 기준을 말하는 %는 뺀다. 지원·환급 같은 말이 그 줄이나 지원내용 어딘가에 있어야 한다."""
    if not _SUPPORT_WORD_RE.search(line) and not _SUPPORT_WORD_RE.search(context):
        return []
    return [m.group() for m in _PERCENT_RE.finditer(line) if not re.search(r"소득|재산|자산|이하|이상|미만|초과", line[max(0, m.start() - 10):m.end() + 4])]


def _lines(text: str) -> list[str]:
    parts = re.split(r"[\r\n]+|(?<=[.!?。])\s+", text or "")
    return [re.sub(r"\s+", " ", p).strip(" -·ㆍ○●▪※\t") for p in parts if p.strip()]


def analyze(service: dict) -> dict | None:
    """제도 하나의 금액 구조를 분석한다. 지원내용에 금액·비율이 없으면 None (현물·서비스 제도)."""
    content = service.get("support_content") or ""
    lines = _lines(content)
    amount_lines = [l for l in lines if _AMOUNT_RE.search(l) or _support_percents(l, content)]
    if not amount_lines:
        return None
    amounts = {re.sub(r"\s+", "", m) for l in amount_lines for m in _AMOUNT_RE.findall(l)}
    percents = {re.sub(r"\s+", "", m) for l in amount_lines for m in _support_percents(l, content)}
    conditional = [l for l in amount_lines if _CONDITION_RE.search(l)]
    text_all = " ".join([content, service.get("selection_criteria") or "", service.get("support_target") or ""])
    if _DISCOUNT_NAME_RE.search(service.get("service_name") or ""):
        kind = "할인형"
    elif len(amounts | percents) >= 3 and len(conditional) >= 2:
        kind = "구간형"
    elif percents and (_CAP_RE.search(content) or _COST_RE.search(content)) and not (len(amounts) >= 3):
        kind = "정률+한도"
    elif _LOAN_RE.search(service.get("service_name") or "") or (_LOAN_RE.search(content) and not re.search(r"월\s*\d|매월|지급|수당", content)):
        kind = "대출형"
    else:
        kind = "정액"
    inputs = [name for name, pat in _INPUT_HINTS if pat.search(text_all)]
    return {
        "kind": kind, "amounts": sorted(amounts)[:8], "percents": sorted(percents)[:8],
        "has_period": bool(_PERIOD_RE.search(content)), "inputs": inputs,
        "sentences": [l[:MAX_SENTENCE_LEN] for l in amount_lines[:MAX_SENTENCES]],
    }


def score(info: dict, demand: int, local: bool = False) -> int:
    """높을수록 규칙을 만들 가치가 크다: 구조 점수 + 기간 단서 + 입력 종류 + 수요(좋음·부분 판정 수) - 지자체 제도 감점."""
    return (BASE_SCORE[info["kind"]] + (10 if info["has_period"] else 0) + 5 * min(len(info["inputs"]), 3) + min(20, 4 * demand)
            - (LOCAL_PENALTY if local else 0))


def demand_counts(events_dir: Path) -> dict[str, int]:
    """검수 기록에서 제도별 '좋음·부분' 판정 수 (질문·검수자별 마지막 판정만). 폴더가 없으면 빈 dict."""
    last: dict[tuple, str] = {}
    for path in sorted(Path(events_dir).glob("*.jsonl")) if Path(events_dir).is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            d = e.get("data") or {}
            if e.get("type") == "judgment" and d.get("service_id"):
                last[(d.get("question_id"), d["service_id"], e.get("reviewer"))] = d.get("label")
    counts: dict[str, int] = {}
    for (_, sid, _), label in last.items():
        if label in ("good", "partial"):
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def load_active(db_path: Path) -> list[dict]:
    """활성 제도 payload 목록. DB를 읽기 전용으로 열어 수집·색인 쪽 잠금이나 기록과 섞이지 않게 한다."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"DB가 없어요: {path} (python -m src.datapack install 로 데이터 팩을 설치하세요)")
    con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        return [json.loads(r[0]) for r in con.execute("SELECT payload FROM services WHERE status='active' ORDER BY service_id")]
    finally:
        con.close()


def load_raw(raw_path: Path) -> list[dict]:
    """수집 원본(`data/raw/serviceList.json`, 공공데이터 API 응답 목록)을 DB payload와 같은 이름의 dict로 바꿔 읽는다. DB가 없을 때의 대안."""
    items = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    return [{"service_id": x.get("서비스ID", ""), "service_name": x.get("서비스명", ""), "agency": x.get("소관기관명", ""),
             "support_content": x.get("지원내용", ""), "support_target": x.get("지원대상", ""), "selection_criteria": x.get("선정기준", ""),
             "how_to_apply": x.get("신청방법", ""), "detail_url": x.get("상세조회URL", "")} for x in items]


def rank(services: list[dict], demand: dict[str, int] | None = None) -> list[dict]:
    demand = demand or {}
    rows = []
    for s in services:
        info = analyze(s)
        if info is None:
            continue
        sid = s.get("service_id", "")
        has_rule = amount_rules.find(sid) is not None
        n = demand.get(sid, 0)
        local = bool(_LOCAL_RE.search((s.get("agency") or "").strip()))
        rows.append({"service_id": sid, "service_name": s.get("service_name", ""), "agency": s.get("agency", ""), "local": local,
                     "has_rule": has_rule, "demand": n, "score": score(info, n, local), **info})
    rows.sort(key=lambda r: (r["has_rule"], -r["score"], r["service_id"]))      # 규칙 없는 제도 먼저, 점수 높은 순
    return rows


def _table(rows: list[dict]) -> str:
    out = ["순위 | 점수 | service_id | 서비스명 (소관기관) | 유형 | 필요 입력(추정) | 수요 | 금액 문장", "--- | --- | --- | --- | --- | --- | --- | ---"]
    for i, r in enumerate(rows, 1):
        mark = " [규칙 있음]" if r["has_rule"] else ""
        out.append(f"{i} | {r['score']} | {r['service_id']} | {r['service_name']}{mark} ({r['agency']}) | {r['kind']} | "
                   f"{', '.join(r['inputs']) or '-'} | {r['demand']} | {' / '.join(r['sentences'][:2])}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="예상 수령액 규칙을 만들 만한 제도 후보를 찾는다 (읽기 전용)")
    ap.add_argument("--db", default=str(config.DB_PATH), help="services.db 경로 (기본: data/services.db)")
    ap.add_argument("--raw", help="DB 대신 수집 원본 serviceList.json을 읽는다")
    ap.add_argument("--events", default=str(config.ROOT / "reviews" / "events"), help="검수 이벤트 폴더 (수요 계산)")
    ap.add_argument("--top", type=int, default=30, help="표에 보일 후보 수")
    ap.add_argument("--kind", choices=KINDS, help="이 유형만")
    ap.add_argument("--json", help="전체 후보를 이 JSON 파일로 저장")
    args = ap.parse_args(argv)
    services = load_raw(Path(args.raw)) if args.raw else load_active(Path(args.db))
    rows = rank(services, demand_counts(Path(args.events)))
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"금액이 있는 활성 제도 {len(rows)}개 (규칙 있음 {sum(r['has_rule'] for r in rows)}개)\n")
    print(_table(rows[:args.top]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
