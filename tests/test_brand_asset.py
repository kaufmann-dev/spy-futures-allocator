from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "app" / "static" / "spy-futures-allocator-mark.svg"


def test_brand_mark_is_self_theming_and_accessibly_named() -> None:
    svg = SVG_PATH.read_text()
    assert 'viewBox="0 0 48 48"' in svg
    assert 'aria-label="SPY Futures Allocator"' in svg
    assert "prefers-color-scheme: dark" in svg
    assert "#000000" in svg
    assert "#ffffff" in svg


def test_templates_reference_the_single_brand_asset() -> None:
    base = (ROOT / "app" / "templates" / "base.html").read_text()
    login = (ROOT / "app" / "auth" / "router.py").read_text()
    dashboard = (ROOT / "app" / "templates" / "dashboard.html").read_text()
    path = "/static/spy-futures-allocator-mark.svg"
    assert base.count(path) == 1
    assert login.count(path) == 1
    assert path not in dashboard
