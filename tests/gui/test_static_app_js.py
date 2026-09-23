"""Regression pin for the Copy-button clipboard handler in app/gui/static/app.js.

The GUI renders several `<button class="btn btn-sm copy" data-copy="...">Copy</button>`
controls (provision_automated/manual/scripted.html, job.html, run_record.html) plus one
`data-copy-target="runlog"` variant (run_record.html's streaming log). All are pure
client-side progressive enhancement, so actual click behavior isn't unit-testable from
pytest (no browser here) -- this just pins that the handler's key pieces (the data-copy
attribute, the data-copy-target fallback, and the legacy execCommand path required for
plain-http/LAN-IP deployments where navigator.clipboard is unavailable) still exist in
the shipped file, so a future refactor can't silently drop them again.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_JS = REPO_ROOT / "app" / "gui" / "static" / "app.js"
README = REPO_ROOT / "README.md"


def test_app_js_wires_the_copy_buttons():
    src = APP_JS.read_text()
    # Reads the literal text to copy off the button.
    assert "data-copy" in src
    # run_record.html's log button copies an element's live text instead of a literal.
    assert "data-copy-target" in src
    # Legacy fallback for plain http on a LAN IP, where navigator.clipboard is undefined
    # (Clipboard API is secure-context-only).
    assert "execCommand" in src
    assert "isSecureContext" in src


def test_readme_no_longer_calls_the_automated_wizard_planned():
    body = README.read_text()
    assert "planned, later phase" not in body
    assert "GUI wizard is planned" not in body


def test_dedicated_bucket_hint_covers_the_empty_suffix_case():
    # updateHint (job-form wizard, Task 8 + Addendum 2026-09-22): a bucket value of
    # exactly `base + "-"` has NO suffix at all -- the server refuses it
    # (routes._dedicated_name_ok requires len(bucket) > len(base) + 1) -- so the
    # hint must stay visible for that value too, not just for ones that don't even
    # start with `base + "-"`.
    src = APP_JS.read_text()
    m = re.search(r"function updateHint\(\) \{.*?\n  \}", src, re.S)
    assert m, "updateHint() not found in app.js"
    assert 'v !== base + "-"' in m.group(0)
