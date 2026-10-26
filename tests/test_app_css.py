"""앱 CSS: 전체 글꼴 규칙이 Streamlit 아이콘 글꼴을 덮어쓰지 않는지 (덮으면 expander 화살표가 'arrow_right' 글자로 보인다)."""
import pytest

pytest.importorskip("streamlit", reason="pip install -r requirements.txt 필요")


def test_icon_font_rule_comes_after_the_global_font_rule():
    import app.main as main

    css = main.CSS
    global_rule = css.index("font-family: 'Pretendard Variable'")
    icon_rule = css.index('[data-testid="stIconMaterial"]')
    assert icon_rule > global_rule                                           # 같은 중요도면 나중 규칙이 이긴다
    assert "Material Symbols Rounded" in css[icon_rule:icon_rule + 400] and "!important" in css[icon_rule:icon_rule + 400]
