from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
SVG_PATH = ROOT / "app" / "static" / "arl5-mark.svg"
DASHBOARD_PATH = ROOT / "app" / "templates" / "dashboard.html"


def test_logo_is_a_self_theming_standalone_svg() -> None:
    source = SVG_PATH.read_text()

    assert source.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert source.rstrip().endswith("</svg>")
    assert 'viewBox="0 0 48 48"' in source
    assert source.count("<path ") == 2
    assert "svg { fill: #000000; }" in source
    assert "@media (prefers-color-scheme: dark)" in source
    assert "svg { fill: #ffffff; }" in source
    assert ' fill="' not in source


def test_logo_is_rendered_only_in_the_dashboard_header() -> None:
    template = DASHBOARD_PATH.read_text()

    header = template.split("</header>", maxsplit=1)[0]
    remainder = template.split("</header>", maxsplit=1)[1]
    assert header.count("/static/arl5-mark.svg") == 1
    assert "/static/arl5-mark.svg" not in remainder
    assert 'alt="" aria-hidden="true"' in header
