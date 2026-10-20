"""eval/label_helper.py (질문 정답 채우기 도구) 점검. 가짜 서비스 데이터와 임시 파일만 쓰므로 네트워크·모델이 필요 없다."""
import json
from datetime import date

import pytest

from eval import label_helper as lh

TODAY = date(2026, 10, 19)


def row(sid, name, agency, deadline="", field="주거·자립", target="", content="", atype=None):
    r = {"서비스ID": sid, "서비스명": name, "소관기관명": agency, "서비스분야": field, "신청기한": deadline,
         "지원대상": target, "지원내용": content}
    if atype:
        r["소관기관유형"] = atype
    return r


ROWS = [
    row("611000000127", "서울시 청년 월세 지원", "서울특별시", "2025.6.11.(수) 10:00 ~ 6. 24.(화)18:00", target="만 19~34세 청년 월세"),
    row("116010000001", "주택금융공사 월세자금보증", "한국주택금융공사", "주택도시기금 주거안정 월세대출 규정에 따름", target="월세 대출 보증"),
    row("648000001055", "경상남도 청년 월세 지원", "경상남도", "접수기관 별 상이", target="청년 월세"),
    row("402000000126", "군포시 청년 전월세 보증금 대출이자 지원", "경기도 군포시", "2026.10.12~2026.10.30", target="청년 전월세"),
    row("315000000269", "강서구 전세피해지원금(청년 월세) 지원", "서울특별시 강서구", "2023.09.06~2027.07.25", target="청년 월세"),
    row("999000000022", "기존주택 전세임대주택 지원사업", "국토교통부", "접수기관 별 상이", target="전세임대"),
    row("569000000379", "세종시 청년 주택임차보증금 이자지원", "세종특별자치시", "금년 접수 마감", target="청년 보증금"),
    row("319000000169", "동작구 전세피해임차인 지원금(월세 실비 지원)", "서울특별시 동작구", "2025-07-14 ~ 예산소진 시까지", target="월세 실비"),
    row("700000000001", "국민취업지원제도", "고용노동부", "상시신청", field="고용·창업", target="구직 중인 청년 취업준비", content="구직촉진수당"),
    row("700000000002", "청년 월세 한시 특별지원", "국토교통부", "2026.01.01~2026.12.31", target="청년 월세 지원"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    q = [
        {"id": "q01", "category": "상황형", "topic": "주거·구직", "question": "월세가 부담되고 취업을 준비하고 있어요", "relevant_service_ids": [],
         "expected_answer": "주거·구직 후보 안내", "expect_no_answer": False, "todo": "정답 지정"},
        {"id": "q02", "category": "지역·연령 조건", "topic": "주거", "question": "서울에 사는 27살인데 월세가 부담돼요", "relevant_service_ids": [],
         "expected_answer": "서울 제도 안내", "expect_no_answer": False, "filters": {"age": 27, "region": "서울"}, "todo": "정답 지정"},
        {"id": "q03", "category": "기한 지난 제도", "topic": "y", "question": "끝난 제도", "relevant_service_ids": [], "expect_no_answer": True, "todo": "기한 확인"},
        {"id": "q04", "category": "상황형", "topic": "z", "question": "이미 채움", "relevant_service_ids": ["700000000001"], "expect_no_answer": False},
    ]
    qpath = tmp_path / "questions.jsonl"
    qpath.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in q), encoding="utf-8")
    cache = tmp_path / "serviceList.json"
    cache.write_text(json.dumps(ROWS, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(lh, "QUESTIONS_PATH", qpath)
    monkeypatch.setattr(lh, "CACHE_PATH", cache)
    monkeypatch.setattr(lh, "BACKUP_DIR", tmp_path / "backups")
    return qpath, tmp_path / "backups"


def scripted(lines):
    it = iter(lines)
    return lambda prompt="": next(it)


def run_session(inputs, **kw):
    out = []
    n = lh.label_session(lh.load_questions(), lh.load_cache(), input_fn=scripted(inputs), print_fn=out.append, today=TODAY, **kw)
    return n, "\n".join(map(str, out))


# --- 지역 ----------------------------------------------------------------------
def test_norm_region():
    assert lh.norm_region("서울") == "서울" and lh.norm_region("서울특별시") == "서울" and lh.norm_region("경기도") == "경기"
    assert lh.norm_region("전북특별자치도") == "전북" and lh.norm_region("제주도") == "제주" and lh.norm_region("경남") == "경남"
    assert lh.norm_region("없는곳") is None and lh.norm_region("") is None and lh.norm_region(None) is None


@pytest.mark.parametrize("agency,expected", [
    ("서울특별시", ("local", "서울")), ("서울특별시 강서구", ("local", "서울")), ("경기도 군포시", ("local", "경기")),
    ("경상남도", ("local", "경남")), ("세종특별자치시", ("local", "세종")), ("전북특별자치도 무주군", ("local", "전북")),
    ("경기주택도시공사", ("local", "경기")),                 # 시도 약칭으로 시작하는 지역 공기업
    ("강서구", ("local", None)),                              # 시도 없이 구만 있으면 지역 미상
    ("국토교통부", ("national", None)), ("한국주택금융공사", ("national", None)), ("고용노동부", ("national", None)),
    ("", ("national", None)),
])
def test_service_scope(agency, expected):
    assert lh.service_scope(row("1", "x", agency)) == expected


def test_agency_type_overrides():
    assert lh.service_scope(row("1", "x", "서울주택도시공사", atype="중앙행정기관")) == ("national", None)
    assert lh.service_scope(row("1", "x", "어떤기관", atype="지방자치단체")) == ("local", None)


# --- 신청기한 ------------------------------------------------------------------
@pytest.mark.parametrize("text,status", [
    ("2025.6.11.(수) 10:00 ~ 6. 24.(화)18:00", "expired"),        # 연도 없는 끝 날짜
    ("2023.09.06~2027.07.25", "open"),
    ("2026.10.12~2026.10.30", "open"),                           # 오늘(10/19)이 기간 안
    ("2026.10.12 10:00 ~ 2026.10.30 18:00", "open"),             # 시각은 날짜로 읽지 않는다
    ("2025-07-14 ~ 예산소진 시까지", "unknown"),
    ("상시신청", "unknown"), ("접수기관 별 상이", "unknown"), ("매년 초 신청시기 공고", "unknown"),
    ("주택도시기금 주거안정 월세대출 규정에 따름", "unknown"), ("", "unknown"),
    ("금년 접수 마감", "expired"),                               # 날짜 없이 마감이라고만 적힌 경우
    ("2026.01.15~2026.12.04", "open"),
    ("2025.12.20 ~ 1.10", "expired"),                            # 연말~연초 (끝 2026-01-10)
])
def test_deadline_status(text, status):
    assert lh.deadline_status(text, TODAY)[0] == status


def test_deadline_end_date_and_today_string():
    assert lh.deadline_status("2025.12.20 ~ 1.10", TODAY)[1] == date(2026, 1, 10)
    assert lh.deadline_status("2026.10.19", "2026-10-19")[0] == "open"        # 마지막 날 당일은 진행 중
    assert lh.deadline_status("2026.10.18", "2026-10-19")[0] == "expired"


# --- 검색 ----------------------------------------------------------------------
def ids(res):
    return [lh.get(r, "id") for r in res.rows]


def test_search_view_scope_and_expired():
    all_ = lh.search(ROWS, ["월세"], view=lh.View(hide_expired=False, today=TODAY))
    assert len(all_.rows) == all_.total == 7
    national = lh.search(ROWS, ["월세"], view=lh.View(scope="national", today=TODAY))
    assert set(ids(national)) == {"116010000001", "700000000002"} and national.hidden_scope == 5
    seoul = lh.search(ROWS, ["월세"], view=lh.View(region="서울", today=TODAY))
    assert "315000000269" in ids(seoul) and "319000000169" in ids(seoul)          # 서울 구 단위 제도
    assert "648000001055" not in ids(seoul) and "402000000126" not in ids(seoul)  # 경남·경기는 숨김
    assert "116010000001" in ids(seoul)                                            # 전국 제도는 남음
    assert "611000000127" not in ids(seoul) and seoul.hidden_expired == 1          # 기한 지난 듯한 서울시 제도 숨김
    shown = lh.search(ROWS, ["월세"], view=lh.View(region="서울", hide_expired=False, today=TODAY))
    assert "611000000127" in ids(shown)


def test_search_local_only_and_and_or_youth():
    local = lh.search(ROWS, ["월세"], view=lh.View(scope="local", hide_expired=False, today=TODAY))
    assert "116010000001" not in ids(local) and "611000000127" in ids(local)
    assert ids(lh.search(ROWS, ["월세", "취업"])) == []                              # 모두 포함 아님
    assert "700000000001" in ids(lh.search(ROWS, ["월세", "취업"], any_term=True))
    assert set(ids(lh.search(ROWS, ["지원"], view=lh.View(youth=True, hide_expired=False, today=TODAY)))) >= {"700000000002"}
    assert lh.search(ROWS, ["월세"], view=lh.View(hide_expired=False, today=TODAY)).rows[0]["서비스명"].find("월세") >= 0


def test_default_view_depends_on_question_region():
    assert lh.default_view({"question": "x"}, TODAY).scope == "national"
    v = lh.default_view({"question": "x", "filters": {"region": "서울"}}, TODAY)
    assert v.region == "서울" and v.scope == "all"


def test_short_line_and_detail_show_scope_and_deadline():
    seoul = ROWS[0]
    line = lh.short_line(seoul, TODAY)
    assert "[서울]" in line and "[기한 지남?]" in line
    assert "[전국]" in lh.short_line(ROWS[1], TODAY) and "[진행중]" in lh.short_line(ROWS[3], TODAY)
    detail = lh.detail_text(seoul, today=TODAY)
    assert "기한이 지난 것으로 추정" in detail and "[범위] 서울" in detail


# --- 대화형 ---------------------------------------------------------------------
def test_region_less_question_shows_national_only_and_saves(env):
    qpath, backups = env
    n, text = run_session(["월세", "1", "ok", "quit"], only=["q01"])
    assert "[지역 없음]" in text and "전국 단위 제도만" in lh.describe_view(lh.default_view({"question": "x"}, TODAY))
    assert "지역·범위 때문에 5건" in text                           # 지자체 제도는 숨겨짐
    q01 = lh.load_questions()[0]
    assert q01["relevant_service_ids"] and q01["relevant_service_names"] and "todo" not in q01
    assert lh.service_scope(next(r for r in ROWS if lh.get(r, "id") == q01["relevant_service_ids"][0]))[0] == "national"
    assert len(list(backups.glob("*.jsonl"))) == 1


def test_region_question_shows_that_region_plus_national(env):
    n, text = run_session(["월세", "quit"], only=["q02"])
    assert "[지역 서울]" in text
    assert "서울특별시 강서구" in text and "경상남도" not in text and "한국주택금융공사" in text


def test_scope_region_expired_commands(env):
    n, text = run_session(["scope all", "월세", "expired on", "월세", "region 경남", "월세", "view", "quit"], only=["q01"])
    assert "지역 구분 없이 전부" in text and "기한 지난 듯한 제도 표시" in text and "경남 제도 + 전국 제도" in text
    assert "[기한 지남?]" in text


def test_warnings_block_save_until_confirmed(env):
    # 지역 없는 질문에 지자체 제도를 고르면 경고하고 확인을 받는다. n이면 저장하지 않는다.
    n, text = run_session(["id:648000001055", "ok", "n", "quit"], only=["q01"])
    assert "지역이 없는 질문인데 지자체 제도" in text and n == 0
    assert lh.load_questions()[0]["relevant_service_ids"] == []
    n, text = run_session(["id:648000001055", "ok", "y"], only=["q01"])
    assert n == 1 and lh.load_questions()[0]["relevant_service_ids"] == ["648000001055"]


def test_warning_other_region_and_expired(env):
    n, text = run_session(["id:648000001055", "id:611000000127", "ok", "n", "quit"], only=["q02"])   # q02는 서울
    assert "다른 지역(경남) 제도" in text and "신청기한이 지난 것으로 보입니다" in text and n == 0


def test_none_keep_and_toggle(env):
    n, text = run_session(["월세", "1 2", "1", "chosen", "clear", "none", "y", "keep"], only=["q01", "q03"])
    assert n == 2 and "해제" in text
    qs = {q["id"]: q for q in lh.load_questions()}
    assert qs["q01"]["expect_no_answer"] is True and "todo" not in qs["q01"] and "todo" not in qs["q03"]


# --- followup / set / none / check / status -------------------------------------
def test_followup_creates_region_question_after_base(env):
    lh.main(["followup", "q01", "--region", "서울특별시", "--age", "27"])
    qs = lh.load_questions()
    assert [q["id"] for q in qs] == ["q01", "q05", "q02", "q03", "q04"]          # 원 질문 바로 뒤, id는 다음 번호
    new = qs[1]
    assert new["filters"] == {"region": "서울", "age": 27} and new["follow_up_of"] == "q01"
    assert new["category"] == "지역 후속 질문" and new["relevant_service_ids"] == [] and "서울에 살아요." in new["question"]
    lh.main(["followup", "q01", "--region", "부산", "--text", "부산 사는데 월세가 힘들어요"])
    assert [q["id"] for q in lh.load_questions()][:3] == ["q01", "q06", "q05"]
    with pytest.raises(SystemExit):
        lh.main(["followup", "q01", "--region", "없는곳"])
    with pytest.raises(SystemExit):
        lh.main(["followup", "q99", "--region", "서울"])


def test_set_warns_but_saves(env, capsys):
    lh.main(["set", "q01", "116010000001", "648000001055", "--today", "2026-10-19"])
    out = capsys.readouterr().out
    assert "지역이 없는 질문인데 지자체 제도" in out
    assert lh.load_questions()[0]["relevant_service_ids"] == ["116010000001", "648000001055"]
    with pytest.raises(SystemExit) as e:
        lh.main(["set", "q01", "없는ID"])
    assert "없는ID" in str(e.value)


def test_none_cmd_and_status(env, capsys):
    lh.main(["none", "q01"])
    assert lh.load_questions()[0]["expect_no_answer"] is True
    lh.main(["followup", "q02", "--region", "경기"])
    capsys.readouterr()
    lh.main(["status"])
    out = capsys.readouterr().out
    assert "(경기)" in out and "정답 지정 1" in out


def test_check_errors_warnings_and_exit_codes(env, capsys):
    qs = lh.load_questions()
    qs[0]["relevant_service_ids"] = ["116010000001", "611000000127", "648000001055"]
    qs[0]["relevant_service_names"] = ["주택금융공사 월세자금보증", "엉뚱한 이름", "경상남도 청년 월세 지원"]
    lh.save_questions(qs)
    with pytest.raises(SystemExit) as e:
        lh.main(["check", "--today", "2026-10-19"])
    out = capsys.readouterr().out
    assert e.value.code == 1 and "이름이 다름" in out and "경고" in out and "기한이 지난" in out and "지역이 없는 질문" in out
    lh.main(["set", "q01", "116010000001"])                             # 문제를 고치면 통과
    with pytest.raises(SystemExit) as e:
        lh.main(["check"])
    assert e.value.code == 0


def test_check_warns_too_many_labels(env, capsys):
    qs = lh.load_questions()
    qs[1]["relevant_service_ids"] = [lh.get(r, "id") for r in ROWS[:6]]
    lh.save_questions(qs)
    with pytest.raises(SystemExit):
        lh.main(["check"])
    assert "정답이 6개입니다" in capsys.readouterr().out


def test_cli_search_prints_hidden_counts(env, capsys):
    lh.main(["search", "월세", "--region", "서울", "--today", "2026-10-19"])
    out = capsys.readouterr().out
    assert "서울 제도 + 전국 제도" in out and "숨김" in out and "[서울]" in out and "[전국]" in out
    lh.main(["search", "월세", "--scope", "national"])
    assert "전국 단위 제도만" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        lh.main(["search", "월세", "--region", "없는곳"])


def test_fields_reports_optional_and_required(env, capsys):
    lh.main(["fields"])
    out = capsys.readouterr().out
    assert "서비스ID" in out and "선택 항목 중 찾지 못한 것" in out and "범위" in out


def test_set_with_source_marks_pending_review_and_human_set_clears(env, capsys):
    lh.main(["set", "q04", "700000000001", "--source", "claude", "--note", "근거", "--confidence", "높음"])
    q = {x["id"]: x for x in lh.load_questions()}["q04"]
    assert q["label_source"] == "claude" and q["reviewed"] is False and q["label_note"] == "근거" and q["label_confidence"] == "높음"
    assert lh.needs_review(q)
    lh.main(["status"])
    assert "사람 검수 대기 1개" in capsys.readouterr().out
    lh.main(["set", "q04", "700000000001"])                                  # 사람이 직접 지정 → 출처 기록 제거
    q = {x["id"]: x for x in lh.load_questions()}["q04"]
    assert not lh.needs_review(q) and "label_source" not in q and "reviewed" not in q


def test_apply_decisions_and_review_ok(env, tmp_path, capsys):
    dec = {"q01": {"ids": ["116010000001"], "confidence": "중간", "note": "월세 보증"},
           "q03": {"ids": [], "no_answer": True, "note": "없음", "confidence": "낮음", "todo": "확인"},
           "q02": {"ids": [], "note": "전국 정답 없음", "todo": "후속 문항으로 평가"}}
    f = tmp_path / "dec.json"
    f.write_text(json.dumps(dec, ensure_ascii=False), encoding="utf-8")
    lh.main(["apply", str(f)])
    qs = {x["id"]: x for x in lh.load_questions()}
    assert qs["q01"]["relevant_service_ids"] == ["116010000001"] and qs["q01"]["relevant_service_names"] == ["주택금융공사 월세자금보증"]
    assert qs["q03"]["expect_no_answer"] is True and qs["q03"]["todo"] == "확인" and lh.state(qs["q03"]) == "no_answer_check"
    assert qs["q02"]["expect_no_answer"] is False and qs["q02"]["todo"] and lh.state(qs["q02"]) == "todo"
    assert all(qs[i]["reviewed"] is False for i in ("q01", "q02", "q03"))
    capsys.readouterr()
    lh.main(["review", "--only", "q01"])
    out = capsys.readouterr().out
    assert "q01" in out and "116010000001" in out and "신뢰도 중간" in out
    lh.main(["review", "--ok", "q01"])
    assert not lh.needs_review({x["id"]: x for x in lh.load_questions()}["q01"])
    lh.main(["review", "--all-ok"])
    assert not any(lh.needs_review(x) for x in lh.load_questions())


def test_apply_unknown_id_changes_nothing(env, tmp_path):
    qpath, _ = env
    before = qpath.read_text(encoding="utf-8")
    f = tmp_path / "dec.json"
    f.write_text(json.dumps({"q01": {"ids": ["116010000001"]}, "q02": {"ids": ["없는ID"]}}), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        lh.main(["apply", str(f)])
    assert "없는ID" in str(e.value) and qpath.read_text(encoding="utf-8") == before


def test_drop_removes_question_and_blocks_when_followups_exist(env, capsys):
    lh.main(["followup", "q01", "--region", "서울"])
    with pytest.raises(SystemExit) as e:
        lh.main(["drop", "q01"])
    assert "후속 문항" in str(e.value) and any(q["id"] == "q01" for q in lh.load_questions())
    lh.main(["drop", "q03"])
    ids = [q["id"] for q in lh.load_questions()]
    assert "q03" not in ids and "q04" in ids
    with pytest.raises(SystemExit):
        lh.main(["drop", "q99"])
    lh.main(["followup", "q04", "--region", "부산"])                    # 번호는 최댓값 다음 → 지운 번호를 재사용하지 않음
    assert "q03" not in [q["id"] for q in lh.load_questions()]
