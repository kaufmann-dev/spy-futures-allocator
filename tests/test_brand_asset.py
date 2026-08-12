from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
SVG_PATH = ROOT / "app" / "static" / "arl5-mark.svg"
BASE_PATH = ROOT / "app" / "templates" / "base.html"
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


def test_logo_is_used_only_as_the_document_favicon() -> None:
    base = BASE_PATH.read_text()
    dashboard = DASHBOARD_PATH.read_text()

    head, body = base.split("</head>", maxsplit=1)
    assert head.count("/static/arl5-mark.svg") == 1
    assert 'rel="icon" type="image/svg+xml" sizes="any"' in head
    assert "/static/arl5-mark.svg" not in body
    assert "/static/arl5-mark.svg" not in dashboard
