"""Row actions in WebUI tables stay in view.

Before: action cells were `white-space: nowrap` at every width, so where a table got
tight the last buttons of a row slid behind its sideways scroll. Measured in headless
Chromium with the German UI: at 1280 px all ten vault buttons (Edit, Delete) were out of
view, at 1440 px the five Delete buttons; at 1024 px the runner, job and service tables
lost theirs too. Now the buttons still sit side by side where there is room, wrap below
each other under 1280 px and in the always-tight vault and runner tables (a single
button never breaks inside), and the vault puts its form below the table under 1400 px.
Measured again afterwards at 13 widths from 1024 to 1920 px: no button out of view.
"""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "app" / "webui_static" / "style.css").read_text(
    encoding="utf-8")


def _rule(selector: str) -> str:
    """The declarations of the top-level rule for exactly this selector."""
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_a_single_button_never_breaks_inside():
    assert "white-space: nowrap" in _rule(".tbl td.actions .btn")


def test_row_actions_wrap_where_a_table_gets_tight():
    assert "white-space: normal" in _rule("#vault-table td.actions, #rn-table td.actions")
    media = re.findall(r"@media \(max-width: (\d+)px\) \{ \.tbl td\.actions \{"
                       r" white-space: normal; \} \}", CSS)
    assert media and int(media[0]) >= 1279


def test_the_vault_form_goes_below_its_table_on_a_laptop_screen():
    media = re.findall(r"@media \(max-width: (\d+)px\) \{ #view-vault \.grid-2 \{"
                       r" grid-template-columns: minmax\(0, 1fr\); \} \}", CSS)
    assert media and int(media[0]) >= 1400
