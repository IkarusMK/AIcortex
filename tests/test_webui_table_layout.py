"""Table layout guard for the WebUI (Services & devices, logs).

Since 1.12.0 the target cell carried ``word-break: break-all``. That lets a cell
shrink to a single character, so the browser squeezed the URL column to a sliver
and every address ran letter by letter over a dozen lines, next to a wide
description. Addresses now wrap only at "/", "?" and "&" with a readable minimum
width, and long descriptions get their own minimum. Measured in a browser at
1280/1440 px: one to three lines per address, no sideways scrolling.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "webui_static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, f"no CSS rule for {selector}"
    return m.group(1)


def test_no_table_cell_breaks_letter_by_letter():
    assert "break-all" not in _rule(".break")


def test_addresses_wrap_at_separators_with_a_minimum_width():
    body = _rule(".url")
    assert "min-width" in body and "max-width" in body
    assert "anywhere" not in body          # a host name stays whole
    assert "function urlText(" in JS and "wbr" in JS
    sv = JS[JS.index("function renderSvTable("):]
    sv = sv[:sv.index("\n}\n")]
    assert "urlText(" in sv and "mono break" not in sv


def test_descriptions_keep_a_readable_minimum():
    body = _rule(".tbl .desc")
    assert "min-width" in body
    assert 'className = "desc"' in JS[JS.index("function renderSvTable("):]


def test_service_buttons_may_stack():
    assert re.search(r"#sv-table td\.actions[^{]*\{[^}]*white-space:\s*normal", CSS)
