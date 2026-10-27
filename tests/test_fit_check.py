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


def test_unfit_recommendations_are_removed_in_order_with_reasons_in_trace(monkeypatch, capsys):
    monkeypatch.setattr(ce, "_chat_json", fake([
        {"id": "A", "fit": "poor", "reason": "또 다른 훈련이고 광고 직군 전용이라 '이것 말고' 요청에 맞지 않음"},
        {"id": "B", "fit": "ok", "reason": ""}, {"id": "C", "fit": "ok", "reason": ""}]))
    trace.set_enabled(True)
    try:
        out = fit_check.drop_unfit(Q, RECS, ["취업준비생이 받을 수 있는 정부지원제도"])
    finally:
        trace.set_enabled(None)
    assert [r.service_id for r in out] == ["B", "C"]
    err = capsys.readouterr().err
    assert "제외   A  글로벌 광고인재 육성" in err and "광고 직군 전용" in err and "유지   B" in err


def test_weak_or_unknown_verdicts_are_ignored(monkeypatch):
    monkeypatch.setattr(ce, "_chat_json", fake([{"id": "A", "fit": "poor", "reason": "별로"},            # 이유가 너무 짧다
                                                 {"id": "ZZ", "fit": "poor", "reason": "후보에 없는 id라서 무시된다"},
                                                 {"id": "B", "fit": "maybe", "reason": "형식이 아닌 값입니다 정말로"}]))
    assert fit_check.drop_unfit(Q, RECS) == RECS


def test_everything_poor_or_failure_or_single_candidate_or_named_program_keeps_all(monkeypatch):
    all_poor = [{"id": r.service_id, "fit": "poor", "reason": "모두 맞지 않는다고 판정한 경우입니다"} for r in RECS]
    monkeypatch.setattr(ce, "_chat_json", fake(all_poor))
    assert fit_check.drop_unfit(Q, RECS) == RECS                                         # 모두 poor면 점검을 믿지 않는다
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

    chunks = {r.service_id: [Chunk(f"{r.service_id}#지원내용", r.service_id, r.service_name, "지원내용", f"{r.service_name}\n내용")] for r in RECS}
    monkeypatch.setattr(generator, "_judge", lambda *a, **k: {})
    monkeypatch.setattr(generator, "_extract_checklists", lambda ev: {})
    monkeypatch.setattr(generator, "_recommendations", lambda *a, **k: list(RECS))
    monkeypatch.setattr(ce, "_chat_json", fake([{"id": "A", "fit": "poor", "reason": "또 다른 훈련이라 이것 말고 요청에 맞지 않음"}]))
    answer = generator._generate(Q, chunks, [], ["취업준비생이 받을 수 있는 정부지원제도"])
    assert [r.service_id for r in answer.recommendations] == ["B", "C"]
