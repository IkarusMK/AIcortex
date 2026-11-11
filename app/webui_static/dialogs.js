/* Modal dialogs. Every create/edit form of the WebUI is a native <dialog> opened with
   showModal(): the browser traps focus, lifts it above the page (top layer) and makes
   the rest inert. Esc, × and Cancel close it — after "Discard changes?" when the form
   was edited; a click on the backdrop never does, so a stray click can't throw typed
   data away. The confirm dialog is a <dialog> as well, so it stacks above an open form.
   While any dialog is open, <html> carries .dlg-lock and the page behind can't scroll.
   Nothing here writes HTML. */

Object.assign(I18N.de, {
  close: "Schließen", done: "Fertig", discard_q: "Änderungen verwerfen?", discard: "Verwerfen",
});
Object.assign(I18N.en, {
  close: "Close", done: "Done", discard_q: "Discard changes?", discard: "Discard",
});

const DLG_FIELDS = "input, select, textarea";
let DLG_OPEN = [];                 // open dialogs, topmost last
let DLG_LAST_CLICK = null;         // opener fallback: Safari doesn't focus a clicked button
const DLG_STATE = new WeakMap();   // dialog → { opener, base, onEscape }

const dlgValue = (f) => (f.type === "checkbox" || f.type === "radio" ? String(f.checked) : f.value);

// What the person could have changed: every field's value, keyed by its id.
function dlgSnapshot(dlg) {
  const fields = [...dlg.querySelectorAll(DLG_FIELDS)].filter((f) => !f.closest("[data-dlg-ignore]"));
  return new Map(fields.map((f, i) => [f.id || `#${i}`, dlgValue(f)]));
}

function dialogDirty(dlg) {
  const st = DLG_STATE.get(dlg);
  if (!st || !st.base) return false;
  const now = dlgSnapshot(dlg);
  return now.size !== st.base.size || [...now].some(([k, v]) => st.base.get(k) !== v);
}

// The current values become "unchanged" — all of them, or one field that was saved on
// its own (the vault's allowed hosts have their own Save button).
function dialogRebase(dlg, field) {
  const st = DLG_STATE.get(dlg);
  if (!st || !dlg.open) return;
  const base = field && field.id && st.base
    ? new Map(st.base).set(field.id, dlgValue(field)) : dlgSnapshot(dlg);
  DLG_STATE.set(dlg, { ...st, base });
}

const topDialog = () => DLG_OPEN[DLG_OPEN.length - 1] || null;

// Scroll lock on <html>, and the toast follows the topmost dialog (see placeToast).
function syncDialogLock() {
  document.documentElement.classList.toggle("dlg-lock", DLG_OPEN.length > 0);
  placeToast();
}

const dlgVisible = (el) => !!el && el !== document.body && el.isConnected && el.getClientRects().length > 0;

// Who opened it: from inside another dialog (Delete → confirm, Esc → "discard?") the
// focused control; otherwise the control just clicked — Safari doesn't focus a clicked
// button, and the focus may still sit in a view that was hidden meanwhile.
function dialogOpener(dlg) {
  const active = document.activeElement;
  const top = topDialog();
  if (top && top !== dlg && top.contains(active) && dlgVisible(active)) return active;
  if (dlgVisible(DLG_LAST_CLICK) && !dlg.contains(DLG_LAST_CLICK)) return DLG_LAST_CLICK;
  return dlgVisible(active) && !dlg.contains(active) ? active : null;
}

function firstField(dlg) {
  return [...dlg.querySelectorAll(".dlg-body input, .dlg-body select, .dlg-body textarea")]
    .find((f) => !f.disabled && !f.readOnly && f.type !== "hidden" && f.getClientRects().length > 0);
}

/* Show a dialog (or refresh one that is already open, e.g. after "reload"), take its
   current values as the unchanged state and focus its first field — or opts.focus. */
function openDialog(dlg, opts = {}) {
  if (!dlg.open) {
    const opener = dialogOpener(dlg);
    dlg.showModal();
    DLG_OPEN = [...DLG_OPEN.filter((d) => d !== dlg), dlg];
    DLG_STATE.set(dlg, { opener, base: null, onEscape: opts.onEscape || null });
    const body = dlg.querySelector(".dlg-body");
    if (body) body.scrollTop = 0;
    syncDialogLock();
  }
  dialogRebase(dlg);
  const target = opts.focus || firstField(dlg) || dlg.querySelector("[data-dlg-close]");
  if (target) target.focus();
}

// Every way out ends here (also a close the browser forced on its own): unlock the
// page and give focus back to whatever opened the dialog.
function dialogClosed(dlg) {
  if (!DLG_OPEN.includes(dlg)) return;
  DLG_OPEN = DLG_OPEN.filter((d) => d !== dlg);
  syncDialogLock();
  const st = DLG_STATE.get(dlg) || {};
  DLG_STATE.delete(dlg);
  // the opener — or, if it was re-rendered or hidden meanwhile, the view's "New" button
  const back = [st.opener, dlg.dataset.return ? $(dlg.dataset.return) : null].find(dlgVisible);
  if (back) back.focus();
}

// Close without asking — after Save or Delete, or once discarding was agreed.
function closeDialog(dlg) {
  if (dlg.open) dlg.close();
  dialogClosed(dlg);
}

// Esc, × and Cancel: ask first when the form was changed.
async function requestCloseDialog(dlg) {
  if (!dlg.open) return true;
  if (dialogDirty(dlg) && !(await confirmModal(t("discard_q"), "discard"))) return false;
  closeDialog(dlg);
  return true;
}

function escapeDialog(dlg) {
  const st = DLG_STATE.get(dlg) || {};
  if (st.onEscape) st.onEscape();
  else requestCloseDialog(dlg).catch((e) => toast(t("toast_error", e.message), false));
}

/* ── confirm dialog ──────────────────────────────────────────────────────── */
let CONFIRM_DONE = null;

function confirmModal(text, okKey = "confirm") {
  if (CONFIRM_DONE) CONFIRM_DONE(false);         // a newer question replaces an open one
  return new Promise((resolve) => {
    const m = $("#modal");
    const off = new AbortController();
    const done = (v) => {
      CONFIRM_DONE = null;
      off.abort();
      closeDialog(m);
      resolve(v);
    };
    $("#modal-text").textContent = text;
    $("#modal-ok").textContent = t(okKey);
    $("#modal-ok").addEventListener("click", () => done(true), { signal: off.signal });
    $("#modal-cancel").addEventListener("click", () => done(false), { signal: off.signal });
    // closed some other way (the browser's own close request): that is a "no"
    m.addEventListener("close", () => { if (!m.open && CONFIRM_DONE === done) done(false); },
      { signal: off.signal });
    CONFIRM_DONE = done;
    // Cancel has the focus: Enter right away must never confirm a deletion.
    openDialog(m, { focus: $("#modal-cancel"), onEscape: () => done(false) });
  });
}

/* ── wiring ──────────────────────────────────────────────────────────────── */
// Esc is ours, not the browser's: its own close request would skip "Discard changes?",
// and a second Esc without a click in between would close every open dialog at once.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.isComposing || !DLG_OPEN.length) return;
  e.preventDefault();
  escapeDialog(topDialog());
}, true);

document.addEventListener("click", (e) => {
  // a click on something that can't take focus (a list row) clears it: no stale opener
  DLG_LAST_CLICK = (e.target.closest && e.target.closest("button, a[href], [tabindex]")) || null;
}, true);

document.addEventListener("DOMContentLoaded", () => {
  $$("dialog").forEach((dlg) => {
    dlg.addEventListener("cancel", (e) => {      // any other close request (e.g. Android back)
      if (!e.cancelable) return;
      e.preventDefault();
      escapeDialog(dlg);
    });
    // "close" arrives a task later — skip it if the dialog was reopened meanwhile
    dlg.addEventListener("close", () => { if (!dlg.open) dialogClosed(dlg); });
  });
  $$("[data-dlg-close]").forEach((b) => b.addEventListener("click", () => {
    requestCloseDialog(b.closest("dialog")).catch((e) => toast(t("toast_error", e.message), false));
  }));
});
