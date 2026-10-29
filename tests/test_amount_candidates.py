"""예상 수령액 규칙 후보 발굴(amount_candidates): 읽기 전용 DB, 유형 분류, 순위, 수요."""
import json
import sqlite3

import pytest

from src.generate import amount_candidates as ac


def svc(sid, name, content, target="", criteria="", agency="가부처"):
    return {"service_id": sid, "service_name": name, "agency": agency, "support_content": content,
            "support_target": target, "selection_criteria": criteria}


FIXED = svc("F1", "정액 수당", "매월 30만원을 6개월간 지급합니다.", target="만 19세 이상 청년", criteria="중위소득 60% 이하")
PERCENT = svc("P1", "비용 환급", "대중교통비의 20%를 환급하며 월 최대 5만원까지 지원합니다.")
TIERED = svc("T1", "구간 지원", "○ 1인 가구: 월 30만원\n○ 2인 가구: 월 40만원\n○ 3인 가구 이상: 월 50만원", criteria="중위소득 기준 소득 이하 가구")
LOAN = svc("L1", "전세 대출", "전세보증금 최대 2억원까지 대출해 드립니다.")
INKIND = svc("N1", "상담 서비스", "취업 상담과 직업훈련 프로그램을 제공합니다.")


def test_analyze_classifies_structure_and_ignores_services_without_money():
    assert ac.analyze(INKIND) is None
    assert ac.analyze(FIXED)["kind"] == "정액" and ac.analyze(FIXED)["has_period"]
    assert ac.analyze(PERCENT)["kind"] == "정률+한도" and "20%" in ac.analyze(PERCENT)["percents"]
    assert ac.analyze(TIERED)["kind"] == "구간형"
    assert ac.analyze(LOAN)["kind"] == "대출형"
    assert "나이" in ac.analyze(FIXED)["inputs"] and "소득" in ac.analyze(FIXED)["inputs"]
    assert "가구원 수" in ac.analyze(TIERED)["inputs"]


def test_sentences_are_verbatim_pieces_of_the_source():
    for s in (FIXED, PERCENT, TIERED):
        text = " ".join(s["support_content"].split())
        assert all(sent.strip() in text for sent in ac.analyze(s)["sentences"])


def test_income_percent_inside_support_text_is_not_a_support_rate():
    only_income = svc("Y", "임대", "○ 월평균소득이 기준 중위소득 150% 이하인 가구에 공급하며 60%는 우선공급합니다.")
    assert ac.analyze(only_income) is None
    assert ac.analyze(svc("Z", "환급", "대중교통비의 20% 환급"))["kind"] == "정률+한도"


def test_facility_discounts_are_the_lowest_kind_and_foundations_count_as_local():
    d = svc("D1", "공영주차장 이용요금 감면", "장애인 50% 감면", agency="종로구시설관리공단")
    assert ac.analyze(d)["kind"] == "할인형"
    row = ac.rank([d, FIXED])
    assert [r["service_id"] for r in row] == ["F1", "D1"] and row[1]["local"]


def test_loan_by_name_and_local_agency_are_ranked_lower():
    assert ac.analyze(svc("Q", "전세자금대출", "월 30만원 이하 이자 지원, 최대 1억원"))["kind"] == "대출형"
    local = svc("K1", "정액 수당", "매월 30만원을 6개월간 지급합니다.", target="만 19세 이상", criteria="소득", agency="서울특별시 종로구")
    central = {**local, "service_id": "K2", "agency": "고용노동부"}
    rows = ac.rank([local, central])
    assert [r["service_id"] for r in rows] == ["K2", "K1"] and rows[1]["local"] and not rows[0]["local"]


def test_selection_criteria_percent_is_not_counted_as_support_amount():
    only_criteria = svc("X", "x", "상담을 제공합니다.", criteria="중위소득 60% 이하이고 재산 3억원 이하")
    assert ac.analyze(only_criteria) is None


def test_rank_puts_tiered_and_in_demand_first_and_marks_existing_rules():
    rows = ac.rank([FIXED, PERCENT, TIERED, LOAN, INKIND], {"F1": 5})
    ids = [r["service_id"] for r in rows]
    assert "N1" not in ids and ids[-1] == "L1"                      # 현물 제외, 대출형은 가장 낮다
    assert ids[0] == "F1"                                          # 수요 5건(+20점)이 구조 점수 차이를 넘는다
    assert ac.rank([TIERED])[0]["score"] > ac.rank([PERCENT])[0]["score"]
    rule = ac.rank([svc("149200005007", "국민취업지원제도", "월 60만원 지급")])
    assert rule[0]["has_rule"] is True
    mixed = ac.rank([svc("149200005007", "국민취업지원제도", "월 60만원 지급"), LOAN])
    assert [r["service_id"] for r in mixed] == ["L1", "149200005007"]      # 이미 규칙이 있으면 뒤로


def test_demand_counts_uses_last_judgment_per_question_service_reviewer(tmp_path):
    def ev(q, sid, label, who="a"):
        return json.dumps({"type": "judgment", "reviewer": who, "data": {"question_id": q, "service_id": sid, "label": label}}, ensure_ascii=False)
    (tmp_path / "a.jsonl").write_text("\n".join([ev("q1", "F1", "bad"), ev("q1", "F1", "good"), ev("q2", "F1", "partial"), ev("q1", "P1", "bad"),
                                                  "깨진 줄", json.dumps({"type": "run", "data": {}})]), encoding="utf-8")
    assert ac.demand_counts(tmp_path) == {"F1": 2}                  # q1은 마지막 판정(good)만, bad는 세지 않는다
    assert ac.demand_counts(tmp_path / "없음") == {}


def _make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE services (service_id TEXT PRIMARY KEY, payload TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, version INTEGER NOT NULL, last_run TEXT)")
    for s, status in rows:
        con.execute("INSERT INTO services VALUES (?,?,?,?,?,?,?)", (s["service_id"], json.dumps(s, ensure_ascii=False), "h", status, None, 1, "r"))
    con.commit()
    con.close()


def test_load_active_is_read_only_and_skips_inactive(tmp_path):
    db = tmp_path / "s.db"
    _make_db(db, [(FIXED, "active"), (PERCENT, "inactive")])
    before = db.read_bytes()
    assert [s["service_id"] for s in ac.load_active(db)] == ["F1"]
    assert db.read_bytes() == before                                 # 읽기만 한다
    with pytest.raises(FileNotFoundError):
        ac.load_active(tmp_path / "없음.db")


def test_cli_prints_table_and_writes_json(tmp_path, capsys):
    db, out = tmp_path / "s.db", tmp_path / "out" / "c.json"
    _make_db(db, [(FIXED, "active"), (TIERED, "active"), (INKIND, "active")])
    assert ac.main(["--db", str(db), "--events", str(tmp_path / "e"), "--top", "5", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "금액이 있는 활성 제도 2개" in text and "구간 지원" in text and "정액 수당" in text and "상담 서비스" not in text
    assert [r["service_id"] for r in json.loads(out.read_text(encoding="utf-8"))] == ["T1", "F1"]
    assert ac.main(["--db", str(db), "--events", str(tmp_path / "e"), "--kind", "정액"]) == 0
    assert "구간 지원" not in capsys.readouterr().out
