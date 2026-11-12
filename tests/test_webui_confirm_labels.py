"""The confirm dialog names what the button does.

confirmModal's default button reads "Ja, löschen" / "Yes, delete" in red. Up to 2.7.1
several questions that delete nothing used that default — among them the sign-in to
an upstream server ("Zur Anmeldung … wechseln?" answered with "Ja, löschen"), the
runner test, signing out everywhere and re-encrypting the vault. Every question that
doesn't delete or revoke something now passes its own button text, and both
languages define it.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "webui_static"
DELETE_LIKE = re.compile(r"(^|_)(del|delete|revoke)(_|$)")


def _calls():
    for f in STATIC.glob("*.js"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"confirmModal\(", text):
            if text[max(0, m.start() - 9):m.start()] == "function ":
                continue
            depth, i, args, start = 1, m.end(), [], m.end()
            while depth:
                ch = text[i]
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                    if depth == 0:
                        args.append(text[start:i])
                elif ch == "," and depth == 1:
                    args.append(text[start:i])
                    start = i + 1
                i += 1
            key = re.search(r't\("([a-z_]+)"', args[0])
            yield f.name, key.group(1) if key else args[0].strip(), [a.strip() for a in args]


def test_only_delete_questions_use_the_delete_button():
    wrong = [(f, key) for f, key, args in _calls()
             if len(args) < 2 and not DELETE_LIKE.search(key)]
    assert not wrong, wrong


def test_the_sign_in_question_offers_to_continue_in_a_neutral_colour():
    calls = [args for _, key, args in _calls() if key == "oa_confirm"]
    assert calls and calls[0][1] == '"oa_go"' and calls[0][2] == '"primary"'


def test_every_button_text_exists_in_both_languages():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    de = app[app.index("I18N = {"):app.index("en: {")] if "en: {" in app else app
    used = {args[1].strip('"') for _, _, args in _calls() if len(args) > 1}
    for key in used:
        assert app.count(f"{key}:") >= 2 or sum(
            (STATIC / n).read_text(encoding="utf-8").count(f"{key}:") for n in
            [p.name for p in STATIC.glob("*.js")]) >= 2, key
