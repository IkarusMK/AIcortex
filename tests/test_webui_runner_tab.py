"""The Runner tab in the static WebUI (index.html, view-runner.js, view-cron.js).

Before: the runner was an admin-only panel inside Scheduled jobs. Now it is a tab of
its own for every signed-in person, and a job shows and picks its runner. Proves:
- the nav has "Runner" without the admin marker, its view exists, the old panel is gone;
- the cron table and form have the runner column and field;
- view-runner.js registers the view, has the same text keys in German and English,
  and never writes HTML from data.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "webui_static"


def _keys(js: str, lang: str) -> set:
    block = re.search(rf"Object\.assign\(I18N\.{lang}, \{{(.*?)\n\}}\);", js, re.S).group(1)
    return set(re.findall(r'^\s*"?([\w-]+)"?:', block, re.M)) | set(
        re.findall(r',\s*"?([\w-]+)"?:\s*"', block))


def test_the_runner_tab_is_for_everyone():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    nav = re.search(r'<a href="#runners"[^>]*>', html).group(0)
    assert 'data-nav="runners"' in nav and "data-admin" not in nav
    assert 'id="view-runners"' in html and 'id="runner-panel"' not in html
    assert 'data-i18n="cron_col_runner"' in html and 'id="cron-runner-sel"' in html


def test_the_runner_view_speaks_both_languages_and_writes_no_html():
    js = (STATIC / "view-runner.js").read_text(encoding="utf-8")
    assert "VIEWS.runners = loadRunners" in js and "innerHTML" not in js
    de, en = _keys(js, "de"), _keys(js, "en")
    assert de == en and {"nav_runners", "rn_cl_step2", "rn_cl_rights", "rn_k_compatible"} <= de
    cron = (STATIC / "view-cron.js").read_text(encoding="utf-8")
    assert _keys(cron, "de") == _keys(cron, "en") and "cron_f_runner" in _keys(cron, "de")
