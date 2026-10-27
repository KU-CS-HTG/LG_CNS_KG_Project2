"""실제 적합성 점검(fit_check)과 이용 중인 제도 이름이 '찾는 도움'에 남지 않게 하는 처리. LLM은 가짜로 바꿔 끼운다."""
from src.common.schema import Recommendation
from src.generate import condition_extractor as ce
from src.generate import fit_check, trace

Q = "나는 지금 K-Digital Training을 전액지원으로 듣고 있는데, 이것 말고도 취업준비생이 받을 수 있는 정부지원제도에 어떤 것들이 있는지 알려줘."


def rec(sid, name, benefit="지원"):
    return Recommendation(name, "기관", "이유", benefit, "신청", service_id=sid, needs_check=["연령 — 만 15세 이상"])


RECS = [rec("A", "글로벌 광고인재 육성", "광고 분야 인재 교육"), rec("B", "국민취업지원제도", "구직촉진수당"), rec("C", "청년도전지원사업", "맞춤형 프로그램")]


def fake(results):
    return lambda system, user: {"results": results}


def ev(**texts):
    """service_id -> 근거 조각 (본문에 texts[service_id]가 들어 있다)."""
    from src.common.schema import Chunk
    return {sid: [Chunk(f"{sid}#지원대상", sid, sid, "지원대상", f"{sid}\n{text}")] for sid, text in texts.items()}


EVIDENCE = ev(A="광고 분야 종사 예정자를 위한 교육 과정", B="구직촉진수당과 취업 상담", C="맞춤형 프로그램")


def test_unfit_recommendations_are_removed_in_order_with_reasons_in_trace(monkeypatch, capsys):
    monkeypatch.setattr(ce, "_chat_json", fake([
        {"id": "A", "fit": "poor", "category": "niche", "quote": "광고 분야 종사 예정자", "reason": "광고 직군 전용이라 일반 취업준비생에게 맞지 않음"},
        {"id": "B", "fit": "ok"}, {"id": "C", "fit": "ok"}]))
    trace.set_enabled(True)
    try:
        out = fit_check.drop_unfit(Q, RECS, ["취업준비생이 받을 수 있는 정부지원제도"], EVIDENCE)
    finally:
        trace.set_enabled(None)
    assert [r.service_id for r in out] == ["B", "C"]
    err = capsys.readouterr().err
    assert "제외   A  글로벌 광고인재 육성" in err and "[niche]" in err and "근거: '광고 분야 종사 예정자'" in err and "유지   B" in err


def test_a_verdict_without_a_quote_found_in_the_right_text_is_discarded(monkeypatch, capsys):
    from src.generate import exclusion

    exclusion.IN_USE_CONTEXT.set([{"이름": "K-디지털 트레이닝", "설명": "신기술 분야 훈련과정 지원 / 국민내일배움카드 발급자"}])
    try:
        monkeypatch.setattr(ce, "_chat_json", fake([
            # 이용 중인 제도 설명에 없는 말로 '중복'이라 한 경우 (K-Digital Training이 취업 지원 서비스를 준다는 구절은 없다)
            {"id": "B", "fit": "poor", "category": "duplicate", "quote": "취업 지원 서비스를 받고", "reason": "이미 취업 지원 서비스를 받고 있어 중복"},
            # 전제 조건 중복: 구절이 이용 중인 제도 설명에 있다 -> 인정
            {"id": "C", "fit": "poor", "category": "duplicate", "quote": "국민내일배움카드 발급자", "reason": "이미 발급받은 카드를 다시 안내하는 것"},
            # niche인데 근거를 후보 원문에서 못 찾음 / 근거 없음 / 범주 없음
            {"id": "A", "fit": "poor", "category": "niche", "quote": "해외 취업 전용", "reason": "특정 업종 전용이라 맞지 않음"}]))
        trace.set_enabled(True)
        try:
            out = fit_check.drop_unfit(Q, RECS, None, EVIDENCE)
        finally:
            trace.set_enabled(None)
        assert [r.service_id for r in out] == ["A", "B"]                                  # C만 빠진다
        assert "근거 구절을 자료에서 확인하지 못해 판정을 버림" in capsys.readouterr().err
        monkeypatch.setattr(ce, "_chat_json", fake([{"id": "A", "fit": "poor", "reason": "범주와 근거 구절이 없는 판정입니다"},
                                                     {"id": "B", "fit": "poor", "category": "niche", "quote": "", "reason": "근거 구절이 비어 있는 판정"}]))
        assert fit_check.drop_unfit(Q, RECS, None, EVIDENCE) == RECS
    finally:
        exclusion.IN_USE_CONTEXT.set([])


def test_weak_or_unknown_verdicts_are_ignored(monkeypatch):
    monkeypatch.setattr(ce, "_chat_json", fake([
        {"id": "A", "fit": "poor", "category": "niche", "quote": "광고 분야 종사 예정자", "reason": "별로"},                      # 이유가 너무 짧다
        {"id": "ZZ", "fit": "poor", "category": "niche", "quote": "광고 분야", "reason": "후보에 없는 id라서 무시된다"},
        {"id": "B", "fit": "maybe", "category": "niche", "quote": "구직촉진수당", "reason": "형식이 아닌 값입니다 정말로"}]))
    assert fit_check.drop_unfit(Q, RECS, None, EVIDENCE) == RECS


def test_everything_poor_or_failure_or_single_candidate_or_named_program_keeps_all(monkeypatch):
    all_poor = [{"id": r.service_id, "fit": "poor", "category": "niche", "quote": q, "reason": "모두 맞지 않는다고 판정한 경우입니다"}
                for r, q in zip(RECS, ("광고 분야 종사 예정자", "구직촉진수당", "맞춤형 프로그램"))]
    monkeypatch.setattr(ce, "_chat_json", fake(all_poor))
    assert fit_check.drop_unfit(Q, RECS, None, EVIDENCE) == RECS                         # 모두 poor면 점검을 믿지 않는다
    assert fit_check.drop_unfit(Q, RECS[:1]) == RECS[:1]                                 # 후보가 하나면 점검하지 않는다
    assert fit_check.drop_unfit("청년희망적금 알려줘", RECS, ["청년희망적금"]) == RECS      # 제도 이름을 콕 집어 물으면 점검하지 않는다
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: (_ for _ in ()).throw(RuntimeError("down")))
    assert fit_check.drop_unfit(Q, RECS) == RECS                                         # 호출 실패


def test_prompt_gets_question_and_candidates(monkeypatch):
    seen = []
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: seen.append((s, u)) or {"results": []})
    fit_check.drop_unfit(Q, RECS)
    assert seen[0][0] is fit_check.PROMPT and "이것 말고도" in seen[0][1] and "글로벌 광고인재 육성" in seen[0][1]


def test_help_that_is_only_the_in_use_program_name_is_dropped():
    raw = {"help": ["K-Digital Training", "취업준비생이 받을 수 있는 정부지원제도"], "in_use": ["K-Digital Training"]}
    got = ce._normalize_conditions(raw, question=Q)
    assert got["in_use"] == ["K-Digital Training"] and got["help"] == ["취업준비생이 받을 수 있는 정부지원제도"]
    only = ce._normalize_conditions({"help": ["K-Digital Training"], "in_use": ["K-Digital Training"]}, question=Q)
    assert "help" not in only                                                          # 이용 중인 제도 이름뿐이면 찾는 도움이 없다
    mixed = ce._normalize_conditions({"help": ["K-Digital Training 말고 다른 지원"], "in_use": ["K-Digital Training"]},
                                     question="K-Digital Training 말고 다른 지원 알려줘 지금 듣고 있어")
    assert mixed["help"] == ["K-Digital Training 말고 다른 지원"]                          # 이름 말고 의미가 남으면 그대로


def test_generate_applies_the_check_before_taking_the_top_five(monkeypatch):
    from src.common.schema import Chunk
    from src.generate import generator

    chunks = {r.service_id: [Chunk(f"{r.service_id}#지원대상", r.service_id, r.service_name, "지원대상", f"{r.service_name}\n광고 분야 종사 예정자 내용")] for r in RECS}
    monkeypatch.setattr(generator, "_judge", lambda *a, **k: {})
    monkeypatch.setattr(generator, "_extract_checklists", lambda ev: {})
    monkeypatch.setattr(generator, "_recommendations", lambda *a, **k: list(RECS))
    monkeypatch.setattr(ce, "_chat_json", fake([{"id": "A", "fit": "poor", "category": "niche", "quote": "광고 분야 종사 예정자", "reason": "광고 직군 전용이라 맞지 않음"}]))
    answer = generator._generate(Q, chunks, [], ["취업준비생이 받을 수 있는 정부지원제도"])
    assert [r.service_id for r in answer.recommendations] == ["B", "C"]


# --- 이용 중인 제도 설명을 점검에 넘기기, 모자란 자리 채우기 ----------------------------------------------------------
def test_in_use_program_text_reaches_the_fit_check(monkeypatch):
    import json
    from src.common.schema import Chunk
    from src.generate import exclusion

    kd = Chunk("K#지원대상", "K", "K-디지털 트레이닝", "지원대상", "K-디지털 트레이닝\n국민내일배움카드 발급자")
    other = Chunk("N#지원내용", "N", "국민취업지원제도", "지원내용", "국민취업지원제도\n구직촉진수당")
    seen = []
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: seen.append((s, u)) or {"analysis": 1, "in_use": [{"name": "K-Digital Training", "aliases": ["K-디지털 트레이닝"], "kind": "직업훈련"}], "candidates": []})
    exclusion.exclude_in_use(Q, {"in_use": ["K-Digital Training"]}, [kd, other])
    ctx = exclusion.IN_USE_CONTEXT.get()
    assert [c["이름"] for c in ctx] == ["K-디지털 트레이닝"] and "국민내일배움카드 발급자" in ctx[0]["설명"]
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: seen.append((s, u)) or {"results": []})
    fit_check.drop_unfit(Q, RECS)
    assert "이용 중인 제도 설명" in json.loads(seen[-1][1]) and "국민내일배움카드 발급자" in seen[-1][1]
    exclusion.exclude_in_use(Q, {}, [kd])                                              # 이용 중인 제도를 말하지 않은 질문은 비운다
    assert exclusion.IN_USE_CONTEXT.get() == []


def _fill_setup(monkeypatch, first, second):
    from src.common.schema import Chunk
    from src.generate import generator

    calls = {"rec": 0, "more": 0}

    def ev(*ids):
        return {i: [Chunk(f"{i}#지원내용", i, f"제도 {i}", "지원내용", f"제도 {i}\n내용")] for i in ids}

    def fake_recs(*a, **k):
        calls["rec"] += 1
        return list(first if calls["rec"] == 1 else second)

    def more():
        calls["more"] += 1
        return ev("X", "Y", "Z")

    monkeypatch.setattr(generator, "_judge", lambda *a, **k: {})
    monkeypatch.setattr(generator, "_extract_checklists", lambda e: {})
    monkeypatch.setattr(generator, "_recommendations", fake_recs)
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: {"results": []})            # 적합성 점검: 모두 ok
    return generator, ev, more, calls


def test_missing_slots_are_filled_from_the_next_batch(monkeypatch):
    generator, ev, more, calls = _fill_setup(monkeypatch, [rec("A", "가"), rec("B", "나")], [rec("A", "가"), rec("X", "엑스"), rec("Y", "와이"), rec("Z", "지"), rec("W", "더블유")])
    evidence = ev("A", "B")
    answer = generator._generate(Q, evidence, [], None, None, more)
    assert [r.service_id for r in answer.recommendations] == ["A", "B", "X", "Y", "Z"]      # 5개까지, 중복(A) 없음, 앞 묶음이 먼저
    assert calls["more"] == 1 and {"X", "Y", "Z"} <= set(evidence)                          # 평가용 근거에도 반영


def test_no_refill_when_enough_or_when_the_first_batch_was_all_rejected(monkeypatch):
    generator, ev, more, calls = _fill_setup(monkeypatch, [rec(i, i) for i in "ABCDE"], [])
    assert len(generator._generate(Q, ev("A"), [], None, None, more).recommendations) == 5 and calls["more"] == 0
    generator, ev, more, calls = _fill_setup(monkeypatch, [], [rec("X", "x")])
    answer = generator._generate(Q, ev("A"), [], None, None, more)
    assert answer.no_evidence and calls["more"] == 0                                         # 범위 밖 질문은 채우지 않고 근거 없음


def test_benefit_kinds_are_passed_so_different_kinds_are_not_called_duplicates(monkeypatch):
    import json
    from src.generate import exclusion

    exclusion.IN_USE_KINDS.set(["직업훈련"])
    exclusion.CANDIDATE_KINDS.set({"B": "소득·수당 지원"})
    seen = []
    monkeypatch.setattr(ce, "_chat_json", lambda s, u: seen.append(u) or {"results": []})
    fit_check.drop_unfit(Q, RECS)
    payload = json.loads(seen[0])
    assert payload["이용 중인 제도 종류"] == ["직업훈련"]
    assert next(c for c in payload["후보"] if c["id"] == "B")["종류"] == "소득·수당 지원"
    assert "종류" not in next(c for c in payload["후보"] if c["id"] == "A")           # 종류를 모르는 후보는 비워 둔다
    assert "같은 취업 지원 분야" in fit_check.PROMPT and "함께 받을 수 있는" in fit_check.PROMPT
    exclusion.IN_USE_KINDS.set([])
    exclusion.CANDIDATE_KINDS.set({})
