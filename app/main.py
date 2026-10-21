"""UI 진입점 (C 담당). answer_question만 호출한다.

실행: python -m app.main   (내부에서 `streamlit run`으로 다시 띄운다)

검색 색인이 비어 있으면(A의 수집·B의 색인 전) tests/fixtures의 샘플 조각으로 검색하는
'샘플 데이터 미리보기' 모드로 자동 전환한다. 색인이 만들어지면 같은 화면이 실제 검색을 쓴다(서버 재시작).
"""
from __future__ import annotations

import html
import re
import sys
import threading
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:          # `streamlit run app/main.py`로 띄워도 src를 찾도록
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from streamlit import runtime  # noqa: E402

from src.common.schema import Answer, Chunk, Filters, Recommendation  # noqa: E402
from src.generate import generator  # noqa: E402
from src.retrieve import hybrid  # noqa: E402

EXAMPLES = [
    "서울에 사는 27살 취업준비생인데 월세가 부담돼요",
    "곧 결혼하는데 신혼부부가 받을 수 있는 주거 지원이 있나요?",
    "대학생인데 학자금 대출 이자가 부담돼요",
    "회사를 그만두고 일자리를 찾고 있어요",
]
DEMO_FIELD_BOOST = 2   # 미리보기 검색에서 관심 분야가 맞는 제도에 더하는 점수 (B의 FIELD_BOOST와 같은 역할)
OFFICIAL_SITES = [("정부24 보조금24", "https://www.gov.kr/portal/rcvfvrSvc/main"), ("복지로", "https://www.bokjiro.go.kr")]

CSS = """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');
html, body, [class*="st-"], .stMarkdown, .stChatMessage, button, input, textarea {
  font-family: 'Pretendard Variable', Pretendard, -apple-system, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif !important;
}
:root {
  --accent: #4f6bed; --accent-soft: rgba(79,107,237,.10);
  --ok: #12a150; --ok-soft: rgba(18,161,80,.12);
  --warn: #d97706; --warn-soft: rgba(217,119,6,.12);
  --line: rgba(128,128,128,.22); --surface: rgba(128,128,128,.05); --muted: rgba(128,128,128,1);
}
[data-testid="stMainBlockContainer"] { max-width: 860px; padding-top: 2.2rem; }
#MainMenu, footer, [data-testid="stAppDeployButton"], [data-testid="stToolbarActions"] { display: none !important; }
header[data-testid="stHeader"] { background: transparent; }
.legend { margin:.15rem 0 .7rem; } .legend .badge { display:inline-block; margin:0 0 .25rem; }
.legend div { font-size:.84rem; opacity:.75; line-height:1.5; }

.hero { padding: 1.6rem 1.7rem; border-radius: 20px; margin-bottom: 1.2rem;
  background: linear-gradient(135deg, rgba(79,107,237,.16), rgba(56,189,248,.10)); border: 1px solid var(--line); }
.hero .pill { display:inline-block; font-size:.78rem; font-weight:600; color:var(--accent);
  background:var(--accent-soft); padding:.22rem .7rem; border-radius:999px; margin-bottom:.7rem; }
.hero h1 { font-size:1.75rem; font-weight:800; line-height:1.3; margin:0 0 .35rem; padding:0; letter-spacing:-.02em; }
.hero p { margin:0; opacity:.78; font-size:1rem; }

.section-label { font-size:.85rem; font-weight:600; opacity:.65; margin:.4rem 0 .5rem; }

.summary { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; margin:.1rem 0 .9rem; font-size:.98rem; }
.chip { font-size:.8rem; font-weight:600; padding:.2rem .6rem; border-radius:999px; background:var(--accent-soft); color:var(--accent); }

[class*="st-key-card_"] { border:1px solid var(--line); background:var(--surface); border-radius:16px;
  padding:1.05rem 1.15rem 1rem; margin:0 0 .85rem; gap:.6rem !important; }
.card-head { display:flex; gap:.75rem; align-items:flex-start; margin-bottom:.75rem; }
.num { flex:none; width:1.75rem; height:1.75rem; border-radius:50%; background:var(--accent); color:#fff;
  font-weight:700; font-size:.9rem; display:flex; align-items:center; justify-content:center; margin-top:.1rem; }
.title { font-size:1.08rem; font-weight:700; line-height:1.4; }
.agency { font-size:.84rem; opacity:.7; margin-top:.1rem; }
.badge { margin-left:auto; flex:none; font-size:.76rem; font-weight:700; padding:.25rem .6rem; border-radius:999px; white-space:nowrap; }
.badge.ok { color:var(--ok); background:var(--ok-soft); }
.badge.check { color:var(--warn); background:var(--warn-soft); }

.reason { font-size:.95rem; line-height:1.6; margin-bottom:.75rem; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:.6rem; margin-bottom:.7rem; }
@media (max-width: 640px) { .grid { grid-template-columns:1fr; } .badge { margin-left:0; } .card-head { flex-wrap:wrap; } }
.box { border-radius:12px; padding:.65rem .8rem; border:1px solid var(--line); }
.box .k { font-size:.78rem; font-weight:700; opacity:.6; margin-bottom:.2rem; }
.box .v { font-size:.92rem; line-height:1.55; }
.deadline { display:inline-block; margin-top:.35rem; font-size:.78rem; font-weight:600; opacity:.8; }
.upcoming { color:var(--accent); background:var(--accent-soft); padding:.05rem .45rem; border-radius:999px; }

/* 자격 체크리스트 (확인이 필요한 조건을 사용자가 스스로 체크) */
[class*="st-key-checklist_"] { border-radius:12px; padding:.65rem .9rem .55rem; background:var(--warn-soft); gap:.35rem !important; }
.ck-title { font-size:.84rem; font-weight:700; color:var(--warn); line-height:1.4; }
.ck-sub { font-size:.78rem; opacity:.7; line-height:1.4; padding-bottom:.7rem; }   /* markdown 블록의 음수 아래 여백 상쇄 */
[class*="st-key-checklist_"] label p { font-size:.9rem; line-height:1.5; }

details.src { border-top:1px dashed var(--line); padding-top:.55rem; }
details.src summary { cursor:pointer; font-size:.85rem; font-weight:600; opacity:.75; list-style:none; }
details.src summary::-webkit-details-marker { display:none; }
details.src summary::before { content:'▸ '; } details.src[open] summary::before { content:'▾ '; }
.quote { border-left:3px solid var(--accent); padding:.25rem .7rem; margin:.5rem 0; font-size:.88rem; line-height:1.55; opacity:.9; }
.links { display:flex; justify-content:flex-end; margin-top:.6rem; }
a.btn { font-size:.84rem; font-weight:600; text-decoration:none !important; color:var(--accent) !important;
  border:1px solid var(--accent); border-radius:999px; padding:.3rem .85rem; }
a.btn:hover { background:var(--accent-soft); }

.callout { border-radius:14px; padding:.8rem 1rem; border:1px solid var(--line); background:var(--accent-soft); margin:.2rem 0 .7rem; font-size:.93rem; }
.callout b { color:var(--accent); }
.disclaimer { font-size:.8rem; opacity:.6; margin-top:.2rem; }
.empty { text-align:center; padding:1.6rem 1rem; border:1px dashed var(--line); border-radius:16px; }
.empty .icon { font-size:2rem; } .empty p { margin:.4rem 0 .9rem; line-height:1.6; }
.empty .links { justify-content:center; gap:.5rem; }
/* 주제 띠 · 이어진 대화 · 이어서 질문 박스 */
.topic { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; padding:.7rem 1rem; margin-bottom:1rem;
  border-radius:14px; border:1px solid var(--line); background:var(--surface); }
.topic .lbl { font-size:.75rem; font-weight:700; color:var(--accent); background:var(--accent-soft); padding:.18rem .55rem; border-radius:999px; }
.topic .q { font-weight:700; font-size:.95rem; flex:1; min-width:12rem; }
.topic .cnt { font-size:.78rem; opacity:.65; }
[class*="st-key-fu_"] { border-left:3px solid var(--accent); margin-left:1.1rem; padding-left:.9rem; }
.fu-tag { display:inline-block; font-size:.75rem; font-weight:700; color:var(--accent); background:var(--accent-soft);
  padding:.12rem .5rem; border-radius:999px; margin-bottom:.3rem; }
.st-key-followbox, .st-key-replybox { border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:14px;
  background:var(--accent-soft); padding:.85rem 1rem .9rem; margin:.3rem 0 .6rem; }
.fb-title { font-weight:700; font-size:.92rem; color:var(--accent); }
.fb-q { font-size:.93rem; margin:.3rem 0 .1rem; }
.fb-sub { font-size:.8rem; opacity:.7; }
.st-key-followbox button, .st-key-replybox button { border-radius:999px !important; font-weight:600 !important; }
.st-key-followbox button { border-color:var(--accent) !important; color:var(--accent) !important; }
[data-testid="stSidebar"] button[kind="primary"] { background:var(--accent) !important; border-color:var(--accent) !important; color:#fff !important; }
[data-testid="stSidebar"] button:disabled { opacity:1 !important; color:var(--accent) !important; border-color:var(--accent) !important;
  background:var(--accent-soft) !important; font-weight:700; }
[data-testid="stSidebar"] [class*="st-key-thread"] button p { text-align:left; }
.relaxed { font-size:.88rem; line-height:1.55; border-radius:12px; padding:.6rem .85rem; margin:.1rem 0 .8rem;
  background:var(--warn-soft); border:1px solid rgba(217,119,6,.25); }
.demo { font-size:.82rem; border-radius:10px; padding:.5rem .75rem; background:var(--warn-soft); color:var(--warn); font-weight:600; }
</style>
"""


# --- 검색 백엔드: B 구현 전이면 샘플 데이터로 대신 ------------------------------------------
@st.cache_resource(show_spinner=False)
def _search_ready() -> bool:
    """실제 검색을 쓸 수 있는가 = 검색 색인에 조각이 있는가. B의 search는 색인이 비면 오류 없이 []를 돌려주므로
    구현 여부만으로는 판단할 수 없다 (그대로 두면 모든 질문이 '근거 없음'이 된다)."""
    try:
        from src.index import vector_store
        return vector_store.count() > 0
    except Exception:            # 미구현·라이브러리 없음·저장소 오류 -> 샘플 미리보기
        return False


@st.cache_resource(show_spinner=False)
def _warm_up() -> threading.Event:
    """서버가 뜰 때 검색 모델·BM25 색인을 미리 불러 둔다 (실제 색인에서 첫 검색만 약 13초 걸렸다).
    화면은 기다리지 않고 바로 뜨고, 그사이 질문이 오면 _ask가 준비가 끝날 때까지 기다린다
    (모델을 두 번 불러오지 않게)."""
    done = threading.Event()

    def run() -> None:
        try:
            hybrid.search("청년 지원", {"today": date.today().isoformat()}, k=1)
        except Exception as e:  # 미리 불러오기에 실패해도 질문할 때 다시 불러온다
            print(f"[app] 검색 미리 불러오기 실패: {e}", file=sys.stderr)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


@st.cache_resource(show_spinner=False)
def _sample_chunks() -> list[Chunk]:
    from tests.fixtures import load_sample_chunks
    return load_sample_chunks()


def _demo_search(query: str, filters: Filters, k: int = 5, mode: str = "hybrid_filter") -> list[Chunk]:
    """미리보기용 간이 검색: 글자 2-gram 겹침 점수 + B와 같은 규칙. 상위 k개 서비스의 전체 항목을 돌려준다
    (실제 B는 서비스당 조각 1개이고, 전체 항목은 C의 _expand_service가 get_service_chunks로 가져온다).
    B 규칙(CLAUDE.md 인터페이스): 거르는 조건은 신청기한·지역·나이, 관심 분야는 거르지 않고 가산점."""
    from src.retrieve import filters as rules
    grams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}  # noqa: E731
    q = grams(re.sub(r"\s+", "", query))
    by_service: dict[str, list[Chunk]] = {}
    for c in rules.apply_filters(_sample_chunks(), filters):      # 기한·지역·나이 (B와 같은 함수)
        by_service.setdefault(c.service_id, []).append(c)

    def score(cs: list[Chunk]) -> float:
        overlap = max(len(q & grams(re.sub(r"\s+", "", c.text))) for c in cs)
        boost = DEMO_FIELD_BOOST if rules.field_matches(cs[0].metadata.get("field"), filters.get("field")) else 0
        return overlap + boost

    scored = sorted(by_service.values(), key=lambda cs: -score(cs))
    return [c for cs in scored[:k] for c in cs]


def _demo_search_with_info(query: str, filters: Filters, k: int = 5, mode: str = "hybrid_filter"):
    """미리보기용 search_with_info: B와 같은 순서(filters.relax)로 결과가 없으면 조건을 하나씩 푼다."""
    from src.retrieve import filters as rules
    current, relaxed = dict(filters), False
    while True:
        chunks = _demo_search(query, current, k, mode)
        softer = rules.relax(current)
        if chunks or softer == current:
            return hybrid.SearchResult(chunks, [], current, relaxed)
        current, relaxed = softer, True


def _ask(question: str) -> Answer:
    if _search_ready():
        _warm_up().wait()
        return generator.answer_question(question)
    original = hybrid.search, hybrid.search_with_info
    hybrid.search, hybrid.search_with_info = _demo_search, _demo_search_with_info
    try:
        return generator.answer_question(question)
    finally:
        hybrid.search, hybrid.search_with_info = original


# --- 렌더링 ----------------------------------------------------------------------------
def _e(s: str) -> str:
    return html.escape(s or "")


def _safe_url(url: str) -> str:
    return url if url.startswith(("https://", "http://")) else ""


def _parse_text(text: str) -> tuple[str, str]:
    """Answer.text에서 상황 요약과 되묻는 질문을 꺼낸다 (generator의 출력 형식)."""
    situation = re.search(r"질문하신 상황\((.+?)\)에 해당할 수 있는", text)
    follow = re.search(r"더 정확히 안내해 드리려면: (.+)", text)
    return (situation.group(1) if situation else ""), (follow.group(1).strip() if follow else "")


def _badge(total: int, checked: int) -> str:
    """카드 머리의 판정 배지. 확인할 조건이 있으면 사용자가 체크리스트에서 체크한 수에 따라 바뀐다.
    모두 체크해도 '해당 가능성 높음'(생성 판정)과 구분해 '체크한 조건 모두 충족'으로 쓴다 (사용자 자가 확인이므로)."""
    if not total:
        return '<span class="badge ok">해당 가능성 높음</span>'
    if checked >= total:
        return '<span class="badge ok">✓ 체크한 조건 모두 충족</span>'
    if checked:
        return f'<span class="badge check">확인 필요 {total}건 중 {checked}건 체크</span>'
    return f'<span class="badge check">확인 필요 {total}건</span>'


def _check_label(item: str) -> str:
    """확인 필요 항목 -> 체크박스 문구 (markdown). '소득 — 본인 연소득 6천만원 이하'는 조건 이름을 굵게."""
    name, sep, criterion = item.partition(" — ")
    return f"**{_md(name.strip())}** — {_md(criterion.strip())}" if sep else _md(item)


def _md(s: str) -> str:
    """markdown 특수문자 이스케이프 (체크박스 문구는 HTML이 아니라 markdown으로 그려진다)."""
    return re.sub(r"([\\`*_\[\]<>#~|$])", r"\\\1", s)


def _card_head(i: int, r: Recommendation, checked: int) -> str:
    how, _, deadline = r.how_to_apply.partition(" / 신청기한: ")
    return f"""
<div class="card-head">
  <div class="num">{i}</div>
  <div><div class="title">{_e(r.service_name)}</div><div class="agency">🏛 {_e(r.agency)}</div></div>
  {_badge(len(r.needs_check), checked)}
</div>
<div class="reason">{_e(r.reason)}</div>
<div class="grid">
  <div class="box"><div class="k">🎁 무엇을 받나요</div><div class="v">{_e(r.benefit)}</div></div>
  <div class="box"><div class="k">📝 어떻게 신청하나요</div><div class="v">{_e(how)}</div>
    {_deadline_html(deadline)}</div>
</div>"""


def _deadline_html(deadline: str) -> str:
    """신청 기간 줄. 아직 접수 전인 제도('신청 예정 · ...')는 강조색으로 구분한다."""
    if not deadline:
        return ""
    mark = f"{generator.UPCOMING_MARK} · "
    if deadline.startswith(mark):
        return (f'<span class="deadline"><span class="upcoming">⏳ {generator.UPCOMING_MARK}</span> '
                f'{_e(deadline[len(mark):])}</span>')
    return f'<span class="deadline">📅 신청 기간 · {_e(deadline)}</span>'


def _card_sources(r: Recommendation) -> str:
    quotes = "".join(f'<div class="quote">{_e(c.quote)}</div>' for c in r.citations)
    url = next((_safe_url(c.detail_url) for c in r.citations if _safe_url(c.detail_url)), "")
    return (f'<details class="src"><summary>근거 원문 {len(r.citations)}건 보기</summary>{quotes}</details>'
            + (f'<div class="links"><a class="btn" href="{_e(url)}" target="_blank" rel="noopener">상세 페이지 열기 ↗</a></div>'
               if url else ''))


def _save_check(key: str) -> None:
    st.session_state.checks[key] = bool(st.session_state.get(key))


def _card(i: int, r: Recommendation, key: str) -> None:
    """제도 카드 1장. 확인이 필요한 조건은 자격 체크리스트로 보여 준다 (사용자가 스스로 해당 여부를 체크).

    체크는 화면 안에서만 쓰고 답변·검색을 바꾸지 않는다. Streamlit은 화면에 그려지지 않은 위젯의 상태를 지우므로
    (다른 주제를 보고 돌아오면 체크가 풀림) 체크 상태는 session_state.checks에 따로 저장한다.
    배지는 카드 머리에 있으므로 체크박스보다 먼저 그리며 저장된 상태를 읽는다."""
    saved = st.session_state.checks
    keys = [f"chk_{key}_{j}" for j in range(len(r.needs_check))]
    checked = sum(1 for k in keys if saved.get(k))
    with st.container(key=f"card_{key}"):
        st.markdown(_card_head(i, r, checked), unsafe_allow_html=True)
        if r.needs_check:
            with st.container(key=f"checklist_{key}"):
                st.markdown('<div class="ck-title">✅ 자격 체크리스트</div>'
                            '<div class="ck-sub">원문 기준을 보고 해당하는 조건을 체크해 보세요.</div>',
                            unsafe_allow_html=True)
                for k, item in zip(keys, r.needs_check):
                    st.checkbox(_check_label(item), value=saved.get(k, False), key=k, on_change=_save_check, args=(k,))
        st.markdown(_card_sources(r), unsafe_allow_html=True)


def _render_answer(answer: Answer, show_follow: bool = True, key: str = "") -> None:
    """key: 이 답변을 구분하는 값 (주제 번호_메시지 번호). 카드 체크리스트 위젯의 key에 쓴다."""
    if answer.no_evidence:
        links = "".join(f'<a class="btn" href="{u}" target="_blank" rel="noopener">{_e(n)} ↗</a>' for n, u in OFFICIAL_SITES)
        body = "<br>".join(_e(line) for line in answer.text.splitlines())
        st.markdown(f'<div class="empty"><div class="icon">🔍</div><p>{body}</p><div class="links">{links}</div></div>',
                    unsafe_allow_html=True)
        return
    situation, follow = _parse_text(answer.text)
    relaxed = next((line for line in answer.text.splitlines() if generator.RELAXED_MARK in line), "")
    if relaxed:                                  # B가 결과가 없어 조건을 푼 경우 (소프트 필터를 투명하게)
        st.markdown(f'<div class="relaxed">🔎 {_e(relaxed)}</div>', unsafe_allow_html=True)
    chips = "".join(f'<span class="chip">{_e(s.strip())}</span>' for s in situation.split(",") if s.strip())
    st.markdown(f'<div class="summary">{chips}<span>상황에 맞는 지원제도 <b>{len(answer.recommendations)}개</b>를 찾았어요.</span></div>',
                unsafe_allow_html=True)
    for i, r in enumerate(answer.recommendations, 1):
        _card(i, r, f"{key}_{i}")
    if follow and show_follow:
        st.markdown(f'<div class="callout"><b>💬 더 정확히 안내해 드릴게요</b><br>{_e(follow)}</div>', unsafe_allow_html=True)
    st.markdown('<div class="disclaimer">※ 최종 신청 자격은 담당 기관에서 확인하세요. 답변은 수집된 공공데이터에 근거하며 '
                '실제와 다를 수 있습니다.</div>', unsafe_allow_html=True)


def _thread() -> list[dict]:
    """지금 화면에 보이는 주제의 대화. 없으면 빈 리스트 (첫 화면)."""
    i = st.session_state.current
    return st.session_state.threads[i] if i is not None else []


def _title(thread: list[dict]) -> str:
    q = thread[0]["content"] if thread else ""
    return q if len(q) <= 22 else q[:21] + "…"


def _sidebar() -> None:
    with st.sidebar:
        st.markdown("### 🧭 청년 지원제도 길잡이")
        st.caption("행정안전부 공공서비스(혜택) 정보(보조금24) 중 개인·가구 지원제도(일자리·주거·교육·생활·결혼·출산·육아 등)를 "
                   "근거로 답합니다.")
        if st.button("＋ 새 질문", use_container_width=True, type="primary"):
            st.session_state.current, st.session_state.follow = None, False
            st.rerun()

        if st.session_state.threads:
            st.markdown("**최근 질문**")
            for i in reversed(range(len(st.session_state.threads))):
                active = i == st.session_state.current
                label = ("▸ " if active else "") + _title(st.session_state.threads[i])
                if st.button(label, key=f"thread{i}", use_container_width=True, disabled=active):
                    st.session_state.current, st.session_state.follow = i, False
                    st.rerun()

        st.markdown("**답변 읽는 법**")
        st.markdown('<div class="legend"><span class="badge ok">해당 가능성 높음</span><div>밝혀 주신 조건으로 자격이 맞아요</div></div>'
                    '<div class="legend"><span class="badge check">확인 필요</span><div>소득·가구 등 더 확인할 조건이 있어요. '
                    '카드의 자격 체크리스트에서 해당하는 조건을 체크해 보세요</div></div>'
                    '<div class="legend"><span class="badge ok">✓ 체크한 조건 모두 충족</span>'
                    '<div>체크리스트를 모두 체크했어요 (스스로 확인한 결과예요)</div></div>',
                    unsafe_allow_html=True)
        st.markdown("**잘 물어보는 팁**")
        st.caption("나이 · 사는 지역 · 지금 상황(구직, 재직, 결혼 등) · 필요한 도움(월세, 학비 등)을 함께 적으면 더 정확해요. "
                   "답변 아래 '이어서 질문하기'로 소득 같은 정보를 덧붙일 수 있어요.")
        if not _search_ready():
            st.markdown('<div class="demo">🧪 샘플 데이터 미리보기 중 — 검색 색인이 만들어지면 실제 데이터로 바뀝니다.</div>',
                        unsafe_allow_html=True)
        st.write("")
        if st.button("🗑 대화 모두 지우기", use_container_width=True):
            st.session_state.threads, st.session_state.current, st.session_state.follow = [], None, False
            st.session_state.checks = {}            # 주제 번호를 다시 쓰므로 체크 상태도 지운다
            st.rerun()


def _render_thread(thread: list[dict]) -> None:
    """첫 질문·답변은 그대로, 이어서 한 질문·답변은 왼쪽 세로줄로 묶어 들여 쓴다 (위 질문과 이어진 대화임을 보이게)."""
    last_answer = max((i for i, m in enumerate(thread) if m["role"] == "assistant"), default=-1)
    group = None
    for i, msg in enumerate(thread):
        if msg["role"] == "user":
            group = st.container(key=f"fu_{st.session_state.current}_{i}") if msg.get("follow") else st.container()
        with (group or st.container()):
            with st.chat_message(msg["role"], avatar="🙋" if msg["role"] == "user" else "🧭"):
                if msg["role"] == "user":
                    tag = '<div class="fu-tag">↳ 위 질문에 이어서</div>' if msg.get("follow") else ""
                    st.markdown(f"{tag}<div>{_e(msg['content'])}</div>", unsafe_allow_html=True)
                else:
                    _render_answer(msg["content"], show_follow=i != last_answer, key=f"{st.session_state.current}_{i}")


def _merged_question(thread: list[dict], new: str) -> str:
    """이어서 질문: 같은 주제의 이전 질문들과 합쳐 하나의 질문으로 보낸다 (나이·지역 등 앞서 밝힌 정보 유지).
    app은 여전히 answer_question 하나만 호출한다 (README 6장)."""
    asked = [m["content"] for m in thread if m["role"] == "user"]
    return " ".join(asked + [new])


def main() -> None:
    st.set_page_config(page_title="청년 지원제도 길잡이", page_icon="🧭", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    st.session_state.setdefault("threads", [])     # 주제별 대화 [[{role, content}, ...], ...]
    st.session_state.setdefault("current", None)   # 화면에 보이는 주제 번호 (None = 첫 화면)
    st.session_state.setdefault("follow", False)   # True면 다음 입력을 지금 주제에 이어서 묻는다
    st.session_state.setdefault("checks", {})      # 자격 체크리스트 체크 상태 {위젯 key: bool}
    if _search_ready():
        _warm_up()                                  # 첫 화면을 보는 동안 검색 준비
    _sidebar()
    thread = _thread()

    pending = None
    if not thread:                                  # 첫 화면: 소개 + 예시 질문
        st.markdown("""
<div class="hero">
  <div class="pill">청년 맞춤 · 공공데이터 기반</div>
  <h1>내게 맞는 정부 지원,<br>평소 말투로 물어보세요</h1>
  <p>상황을 적으면 받을 수 있는 지원제도를 근거와 신청 방법까지 함께 찾아드려요.</p>
</div>""", unsafe_allow_html=True)
        st.markdown('<div class="section-label">이렇게 물어볼 수 있어요</div>', unsafe_allow_html=True)
        cols = st.columns(2)
        for i, ex in enumerate(EXAMPLES):
            if cols[i % 2].button(ex, key=f"ex{i}", use_container_width=True):
                pending = ex

    if thread:
        n_follow = sum(1 for m in thread if m.get("follow"))
        st.markdown(f'<div class="topic"><span class="lbl">📌 지금 보고 있는 질문</span>'
                    f'<span class="q">{_e(thread[0]["content"])}</span>'
                    + (f'<span class="cnt">이어진 질문 {n_follow}개</span>' if n_follow else "") + '</div>',
                    unsafe_allow_html=True)
    _render_thread(thread)
    _answer_pending()
    following = st.session_state.follow and bool(thread)
    if thread and not pending and thread[-1]["role"] == "assistant":
        if following:
            with st.container(key="replybox"):
                c1, c2 = st.columns([5, 1], vertical_alignment="center")
                c1.markdown(f'<div class="fb-title">↳ 위 질문에 이어서 묻는 중</div>'
                            f'<div class="fb-q">“{_e(thread[0]["content"])}”</div>'
                            f'<div class="fb-sub">아래 입력창에 추가 정보나 궁금한 점을 적어 주세요. 앞의 내용과 합쳐서 다시 찾아드려요.</div>',
                            unsafe_allow_html=True)
                if c2.button("✕ 취소", use_container_width=True):
                    st.session_state.follow = False
                    st.rerun()
        else:
            last = thread[-1]["content"]
            _, follow_q = _parse_text(last.text) if not last.no_evidence else ("", "")
            # 근거 없음이면 소득 같은 정보를 덧붙여도 결과가 같다 -> 표현·상황을 바꿔 묻도록 안내
            sub = ("찾는 지원을 다른 말로 바꾸거나, 지금 상황(나이·지역·필요한 도움)을 덧붙여 다시 물어봐 주세요."
                   if last.no_evidence else "소득·가구처럼 더 알려 주실 정보가 있으면 덧붙여 주세요.")
            with st.container(key="followbox"):
                st.markdown('<div class="fb-title">💬 이 답변에 이어서 물어볼 수 있어요</div>'
                            + (f'<div class="fb-q">{_e(follow_q)}</div>' if follow_q else "")
                            + f'<div class="fb-sub">{sub}</div>',
                            unsafe_allow_html=True)
                if st.button("↳ 이어서 질문하기"):
                    st.session_state.follow = True
                    st.rerun()

    placeholder = "이어서 입력하세요 (예: 소득은 월 150만 원이에요)" if following \
        else "새 질문을 입력하세요 (예: 경기도 사는 25살 직장인인데 전세 대출 지원이 있나요?)"
    question = st.chat_input(placeholder) or pending
    if not question:
        return

    if following:
        asked = _merged_question(thread, question)
    else:                                           # 새 주제: 화면을 새로 시작 (이전 주제는 사이드바 '최근 질문')
        st.session_state.threads.append([])
        st.session_state.current = len(st.session_state.threads) - 1
        asked = question
    _thread().append({"role": "user", "content": question, "follow": following})
    st.session_state.follow = False                 # 기본은 다시 '새 질문'
    st.session_state.asking = asked
    st.rerun()                                      # 질문 말풍선이 있는 새 화면을 먼저 그린 뒤 답변을 만든다


def _answer_pending() -> None:
    """직전 실행에서 받은 질문에 답한다. 화면에는 이미 지금 주제의 대화와 새 질문이 그려져 있다."""
    asked = st.session_state.pop("asking", None)
    if not asked:
        return
    thread = _thread()
    box = st.container(key="fu_pending") if thread and thread[-1].get("follow") else st.container()
    with box, st.chat_message("assistant", avatar="🧭"):
        try:
            with st.spinner("상황에 맞는 지원제도를 찾고 있어요…"):
                answer = _ask(asked)
        except Exception as e:  # 사용자에게는 짧게, 원인은 펼쳐서
            st.error("답변을 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.")
            with st.expander("오류 내용"):
                st.code(repr(e))
            thread.pop()
            if not thread:
                st.session_state.threads.pop(st.session_state.current)
                st.session_state.current = None
            return
    thread.append({"role": "assistant", "content": answer})
    st.rerun()


if __name__ == "__main__":
    if runtime.exists():
        main()
    else:                    # python -m app.main -> streamlit run app/main.py
        from streamlit.web import cli as stcli
        sys.argv = ["streamlit", "run", str(Path(__file__).resolve())]
        sys.exit(stcli.main())
