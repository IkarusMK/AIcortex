"""Create/edit forms in the WebUI open as modal dialogs.

Before (2.6.1): every editor sat inline at the bottom or the side of a long list — the
vault and agent-key forms beside their tables (on a laptop the vault form slid below
the table), the skill and note editors next to their lists, the cron, runner, service,
user and playbook editors under their tables. Opening one meant scrolling to find it.
Now each one is a native <dialog> opened with showModal() — focus trap, Esc, top layer:
its title names it, × / Esc / Cancel close it (after "Discard changes?" when it was
edited) and a click on the backdrop does not. The dialog is compact (540 px, 780 px for
long texts, height by content up to 85vh); the form scrolls inside it while the title
and the Save/Delete row stay in view; short fields pair up; on phones a bottom sheet.

Every <dialog> but the confirm question ("modal") is such a form dialog — also the two
upstream sign-in dialogs (Access, and the paste step) that 2.7.0 first built in a look
of their own, opened and closed past dialogs.js (no "Discard changes?", no focus return).

Static guard over index.html, the scripts and style.css (no browser needed).
"""
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "webui_static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
SCRIPTS = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.js"))}
ALL_JS = "\n".join(SCRIPTS.values())
DIALOGS_JS = SCRIPTS.get("dialogs.js", "")

EDITORS = {  # dialog id → the form it holds
    "vault-editor": "form-secret", "cl-editor": "form-agentkey", "sk-editor": "form-skill",
    "mem-editor": "form-mem", "cron-editor": "form-cron", "rn-editor": "form-runner",
    "sv-editor": "form-sv", "us-editor": "form-user", "pb-start-form": "form-pb-start",
    "pb-def-editor": "form-pb-def", "oa-paste": "form-oa-paste", "oa-access": "form-oa-access",
}
CONFIRM = "modal"          # the one dialog that is a question, not a form
# Lists whose form used to be always open beside them: they now get a "New" button.
NEW_BUTTONS = {"view-vault": "btn-vault-new", "view-clients": "btn-ak-new", "view-users": "btn-user-new"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def classes(self):
        return set((self.attrs.get("class") or "").split())

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def iter(self):
        yield self
        for child in self.children:
            yield from child.iter()


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = Node("#root", [], None)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs, self.stack[-1]))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return


def _tree():
    parser = _Tree()
    parser.feed(HTML)
    return parser.root


TREE = _tree()
IDS = {n.attrs["id"]: n for n in TREE.iter() if n.attrs.get("id")}
FORM_DIALOGS = [n for n in TREE.iter() if n.tag == "dialog" and n.attrs.get("id") != CONFIRM]


def _with_class(node, cls):
    return [n for n in node.iter() if cls in n.classes()]


# ── markup ───────────────────────────────────────────────────────────────────
def test_every_form_sits_in_a_modal_dialog():
    forms = [n for n in TREE.iter() if n.tag == "form"]
    assert {f.attrs.get("id") for f in forms} >= set(EDITORS.values())
    for form in forms:
        assert any(a.tag == "dialog" for a in form.ancestors()), form.attrs.get("id")
    # no editor (…-editor) is a plain panel any more, and no dialog escapes this guard
    for node_id, node in IDS.items():
        if node_id.endswith("-editor"):
            assert node.tag == "dialog", node_id
    assert {n.attrs.get("id") for n in TREE.iter() if n.tag == "dialog"} == set(EDITORS) | {CONFIRM}


def test_each_dialog_has_a_title_a_close_button_a_scrolling_body_and_a_footer():
    assert FORM_DIALOGS and all(d.attrs.get("id") for d in FORM_DIALOGS)
    for dlg in FORM_DIALOGS:                       # every dialog but the confirm question
        dlg_id = dlg.attrs["id"]
        form_id = EDITORS.get(dlg_id)
        assert form_id, f"{dlg_id}: a dialog outside the guard (add it to EDITORS)"
        assert "dlg" in dlg.classes() and "hidden" not in dlg.classes(), dlg_id
        title = IDS.get(dlg.attrs.get("aria-labelledby") or "")
        assert title is not None and dlg in title.ancestors() and title.tag in {"h2", "h3"}, dlg_id
        heads, bodies, feet = (_with_class(dlg, c) for c in ("dlg-head", "dlg-body", "dlg-foot"))
        assert len(heads) == len(bodies) == len(feet) == 1, dlg_id
        assert heads[0] in title.ancestors(), dlg_id
        close = [n for n in heads[0].iter() if "data-dlg-close" in n.attrs]
        assert close and close[0].tag == "button" and close[0].attrs.get("type") == "button", dlg_id
        assert close[0].attrs.get("data-i18n-aria") == "close", dlg_id         # aria-label DE/EN
        assert bodies[0] in IDS[form_id].ancestors(), dlg_id
        submit = [n for n in feet[0].iter() if n.tag == "button" and n.attrs.get("form") == form_id
                  and n.attrs.get("type", "submit") == "submit"]
        assert submit, f"{dlg_id}: no Save button in the footer"
        cancel = [n for n in feet[0].iter() if "data-dlg-close" in n.attrs]
        assert cancel and cancel[0].attrs.get("type") == "button", dlg_id
        assert dlg.attrs.get("data-return", "#")[1:] in IDS, dlg_id           # focus fallback


def test_lists_that_had_an_open_side_form_get_a_new_button():
    for view, button in NEW_BUTTONS.items():
        node = IDS.get(button)
        assert node is not None and node.tag == "button", button
        head = next(a for a in node.ancestors() if a.tag == "header")
        assert "view-head" in head.classes() and IDS[view] in head.ancestors(), button
        assert f'$("#{button}").addEventListener("click"' in ALL_JS, button
    # the extra button must not push a view head past a phone's width (Clients did: 455 px)
    assert "flex-wrap: wrap" in _rule(".view-head")
    # and the forms left their views: no form in any view section any more
    for section in (n for n in TREE.iter() if n.tag == "section"):
        assert not [n for n in section.iter() if n.tag == "form"], section.attrs.get("id")


def test_the_confirm_dialog_is_a_dialog_too_so_it_stacks_above_a_form():
    modal = IDS["modal"]
    assert modal.tag == "dialog" and modal.attrs.get("aria-labelledby") == "modal-text"
    assert "hidden" not in modal.classes()
    # the toast lives in the top layer, so it stays readable above an open dialog, and
    # inside the topmost dialog: outside it the page is inert and no screen reader hears it
    assert IDS["toast"].attrs.get("popover") == "manual" and "showPopover" in SCRIPTS["app.js"]
    assert "function placeToast(" in SCRIPTS["app.js"] and "topDialog() || document.body" in SCRIPTS["app.js"]


def _code(js: str) -> str:
    """The script without its comments (they may well say "never innerHTML")."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)(^|\s)//\s.*$", "", js)


def test_no_markup_from_strings_and_no_inline_code():
    for name, text in SCRIPTS.items():
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", 'setAttribute("style"'):
            assert sink not in _code(text), f"{name}: {sink}"
    for node in TREE.iter():
        assert "style" not in node.attrs, node.tag                        # CSP: style-src 'self'
        assert not [a for a in node.attrs if a.startswith("on")], node.tag
        if node.tag == "script":
            assert node.attrs.get("src"), "inline <script>"                # CSP: script-src 'self'
    order = re.findall(r'<script src="/ui/assets/([\w.-]+)\?v=', HTML)
    assert order[:2] == ["app.js", "dialogs.js"] and set(order) == set(SCRIPTS)
    assert '"dialogs.js": "application/javascript' in (ROOT / "app" / "webui.py").read_text(encoding="utf-8")


# ── behaviour (JavaScript) ───────────────────────────────────────────────────
def _keys(js: str, lang: str) -> set:
    """Text keys of one language: `Object.assign(I18N.de, {…})` or app.js' `de: {…}`."""
    block = (re.search(rf"Object\.assign\(I18N\.{lang}, \{{(.*?)\n\}}\);", js, re.S)
             or re.search(rf"\n  {lang}: \{{(.*?)\n  \}},", js, re.S))
    assert block, f"no {lang} texts"
    return set(re.findall(r'(?:^|,)\s*"?([\w-]+)"?:\s*"', block.group(1), re.M))


def test_dialogs_open_modally_and_close_only_on_request():
    assert "showModal()" in DIALOGS_JS and "function openDialog(" in DIALOGS_JS
    # only dialogs.js opens and closes a dialog — so every one gets "Discard changes?",
    # the scroll lock and the focus return (the sign-in dialogs called showModal/close)
    for name, text in SCRIPTS.items():
        if name != "dialogs.js":
            code = _code(text)
            assert "showModal(" not in code and not re.search(r"\b(dlg|dialog)\.close\(", code), name
    assert "onEscape" not in "".join(_code(t) for n, t in SCRIPTS.items() if n != "dialogs.js")
    for dlg_id in (d.attrs["id"] for d in FORM_DIALOGS):
        assert re.search(rf'openDialog\(\$\("#{dlg_id}"\)', ALL_JS), f"{dlg_id} is never opened"
        # a dialog hidden by class would stay modal yet invisible — the page would freeze
        assert not re.search(rf'\$\("#{dlg_id}"\)\.classList\.\w+\("hidden"', ALL_JS), dlg_id
        assert f'$("#{dlg_id}").scrollIntoView' not in ALL_JS, dlg_id
    # Esc, × and Cancel all go through requestCloseDialog, which asks first when edited
    assert re.search(r'e\.key !== "Escape"', DIALOGS_JS) and "e.preventDefault()" in DIALOGS_JS
    assert 'addEventListener("cancel"' in DIALOGS_JS and "[data-dlg-close]" in DIALOGS_JS
    ask = DIALOGS_JS[DIALOGS_JS.index("async function requestCloseDialog("):]
    ask = ask[:ask.index("\n}\n")]
    assert "dialogDirty(" in ask and 'confirmModal(t("discard_q")' in ask
    # a click on the backdrop never closes (no light dismiss, no target === dialog check)
    assert "closedby" not in HTML and not re.search(r"\.target === (dlg|dialog)\b", ALL_JS)
    # focus: first field on open, back to the opener on close; scroll lock is a class on <html>
    assert "function firstField(" in DIALOGS_JS and "opener" in DIALOGS_JS
    assert 'document.documentElement.classList.toggle("dlg-lock"' in DIALOGS_JS
    assert "function confirmModal(" in DIALOGS_JS and "function confirmModal(" not in SCRIPTS["app.js"]
    assert 'el.setAttribute("aria-label"' in SCRIPTS["app.js"]                  # data-i18n-aria


def test_dialog_texts_exist_in_both_languages():
    de, en = _keys(DIALOGS_JS, "de"), _keys(DIALOGS_JS, "en")
    assert de == en and {"close", "discard_q", "discard"} <= de
    assert "Änderungen verwerfen?" in DIALOGS_JS and "Discard changes?" in DIALOGS_JS
    app_de, app_en = _keys(SCRIPTS["app.js"], "de"), _keys(SCRIPTS["app.js"], "en")
    assert {"vault_new", "cl_new", "us_new", "us_new_title", "us_edit_title"} <= app_de & app_en


# ── style ────────────────────────────────────────────────────────────────────
def _rule(selector: str) -> str:
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, f"no rule for {selector}"
    return match.group(1)


def _blocks(at_rule: str) -> list:
    """The bodies of every `@media …` / `@keyframes …` block with exactly this prelude."""
    out, start = [], 0
    while (i := CSS.find(at_rule + " {", start)) >= 0:
        j, depth = CSS.index("{", i), 0
        for k in range(j, len(CSS)):
            depth += {"{": 1, "}": -1}.get(CSS[k], 0)
            if depth == 0:
                out.append(CSS[j + 1:k])
                break
        start = j + 1
    return out


def _px(prop: str, body: str) -> int:
    match = re.search(prop + r":\s*min\((\d+)px", body)
    assert match, f"no {prop}: min(…px, …)"
    return int(match.group(1))


def test_the_dialog_is_compact_and_its_form_scrolls_inside():
    # Compact, not page-sized (owner feedback on the first version: 760/960 px wide and
    # up to 90vh tall was "as big as the whole page"). Height follows the content.
    # Centred: the global `* { margin: 0 }` reset would pin a dialog to the top-left.
    assert "margin: auto" in _rule(".dlg, .modal-card")
    dlg = _rule(".dlg")
    assert 520 <= _px("max-width", dlg) <= 560 and re.search(r"max-height:\s*85vh", dlg)
    assert not re.search(r"(?<![-\w])(min-)?height:", dlg)                # no fixed or minimum height
    assert 760 <= _px("max-width", _rule(".dlg.wide")) <= 800              # skills, notes, playbooks
    opened = _rule(".dlg[open]")
    assert "display: flex" in opened and "flex-direction: column" in opened
    body = _rule(".dlg-body")
    assert "overflow-y: auto" in body and "min-height: 0" in body
    assert "container-type: inline-size" in body                           # columns follow the dialog
    head, foot = _rule(".dlg-head"), _rule(".dlg-foot")
    assert "position: sticky" in head and "top: 0" in head
    assert "position: sticky" in foot and "bottom: 0" in foot              # Save/Delete stay visible


def test_fields_are_compact_and_short_ones_sit_side_by_side():
    label = re.search(r"margin:\s*(\d+)px 0 (\d+)px", _rule(".dlg label"))
    assert label and int(label.group(1)) < 13 and int(label.group(2)) <= 5   # tighter than the page
    assert "min-height: 0" in _rule(".dlg textarea.inp")                   # rows decide, not 130px
    dialogs = [n for n in TREE.iter() if n.tag == "dialog"]
    areas = [a for d in dialogs for a in d.iter() if a.tag == "textarea"]
    assert areas and all(int(a.attrs.get("rows", "0")) in (3, 4) for a in areas), \
        [(a.attrs.get("id"), a.attrs.get("rows")) for a in areas]
    for name in ("view-registry.js", "view-playbook-lib.js"):              # textareas built in JS
        assert all(int(n) <= 4 for n in re.findall(r"\.rows = (\d+)", SCRIPTS[name])), name
    two = _rule(".dlg .row-2, .dlg .form-fields")
    assert "repeat(2, minmax(0, 1fr))" in two
    narrow = "".join(_blocks("@container (max-width: 479px)"))
    assert ".dlg .row-2" in narrow and "grid-template-columns: minmax(0, 1fr)" in narrow
    assert "grid-column: 1 / -1" in _rule(".dlg .fld.full")
    for dlg_id in ("vault-editor", "cl-editor", "sk-editor"):              # short fields paired
        assert _with_class(IDS[dlg_id], "row-2"), dlg_id
    for name in ("view-registry.js", "view-playbook-lib.js"):              # schema/stage fields too
        assert '"fld full"' in SCRIPTS[name] and '"fld"' in SCRIPTS[name], name


def test_a_bottom_sheet_on_phones():
    sheet = "".join(_blocks("@media (max-width: 639px)"))
    assert ".dlg" in sheet and "margin: auto 0 0" in sheet                  # sits at the bottom
    assert re.search(r"max-height:\s*92vh", sheet) and re.search(r"border-radius: \d+px \d+px 0 0", sheet)
    assert not re.search(r"(?<![-\w])height: 100%", sheet)                  # no forced full screen


def test_backdrop_motion_focus_and_scroll_lock():
    backdrop = re.search(r"(?m)^[^{}]*\.dlg::backdrop[^{]*\{([^}]*)\}", CSS)
    assert backdrop and "backdrop-filter" in backdrop.group(1)
    frames = _blocks("@keyframes dlg-in")
    assert frames
    props = set(re.findall(r"([\w-]+)\s*:", frames[0]))
    assert props and props <= {"opacity", "transform"}                    # compositor-only motion
    reduced = "".join(_blocks("@media (prefers-reduced-motion: reduce)"))
    assert ".dlg[open]" in reduced and "animation: none" in reduced
    assert "overflow: hidden" in _rule("html.dlg-lock body")
    assert re.search(r"\.dlg-x:focus-visible[^{]*\{[^}]*outline", CSS)
    assert re.search(r"\.btn:focus-visible[^{]*\{[^}]*outline", CSS)
