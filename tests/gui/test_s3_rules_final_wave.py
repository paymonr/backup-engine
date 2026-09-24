# tests/gui/test_s3_rules_final_wave.py — GUI side of the Phase B/C final fix wave (findings I4,
# M3, M4, M5, M10, M11, P2, P5): console-rule caps on Setup/job page/wizard, honest tier and
# preview copy, the partial-apply flash, the pre-first-check overview, a hand-edited applied
# record, the acknowledge route, and screen() reusing setup_row's computation.
import html
import json
import re
from pathlib import Path

import pytest
from app.engine import lifecycle
from app.gui import s3_rules
from tests.gui.test_s3_rules_screen import (BASE, JOBS, REAL, _applied, _csrf, _flashes,  # noqa: F401
                                            cfg, client, no_aws)

GUIDED = [{"ID": "expire-media", "Status": "Enabled", "Filter": {"Prefix": "media/"},
           "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}]


def _live(cfg, rules, bucket=BASE):
    lifecycle.save_live(cfg["CACHE_DIR"], bucket, rules, versioning="on")


def _ok(cfg, bucket=BASE):
    lifecycle.set_status(cfg["CACHE_DIR"], bucket, "ok")


# --- I4: a console rule that removes versions sooner than the app's rule ----------------------

CAP = "A rule you added in the AWS console (expire-media) removes old versions after 30 days"


def test_the_setup_row_warns_about_a_console_rule_capping_history(cfg):
    _applied(cfg)
    _live(cfg, GUIDED + list(lifecycle.desired(BASE, BASE, JOBS, {}).rules.values()))
    _ok(cfg)
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "warn" and row["fix_url"] == "/setup/storage"
    assert row["sentence"] == CAP + " — delete it there to keep the longer history"


def test_no_warning_when_the_console_rule_keeps_at_least_as_long(cfg):
    _applied(cfg)
    long = [dict(GUIDED[0], NoncurrentVersionExpiration={"NoncurrentDays": 365})]
    _live(cfg, long + list(lifecycle.desired(BASE, BASE, JOBS, {}).rules.values()))
    _ok(cfg)
    assert s3_rules.setup_row(cfg)["state"] == "ok"


def test_the_job_page_notes_the_console_rule(client, cfg):
    from tests.gui.test_vocabulary import forbidden_hits, mono_violations
    _applied(cfg)
    _live(cfg, GUIDED)
    _ok(cfg)
    body = client.get("/jobs/manga").get_data(as_text=True)
    line = re.search(r"<dd data-s3-history>.*?</dd>", body, re.S).group(0)
    text = html.unescape(re.sub(r"<[^>]+>", "", line))
    assert "A rule you added in the AWS console (expire-media) removes old versions after 30 days" in text
    assert "delete it there to keep the longer history" in text
    assert forbidden_hits(line) == [] and mono_violations(line) == []
    # Snapshot's undo window (30) isn't shortened by a 30-day rule on media/ -- no note there
    body = client.get("/jobs/appdata_backups").get_data(as_text=True)
    assert "A rule you added in the AWS console" not in body


def test_the_wizard_notes_the_console_rule(client, cfg):
    _applied(cfg)
    _live(cfg, GUIDED)
    for url in ("/jobs/manga/edit", "/jobs/new"):
        body = html.unescape(re.sub(r"<[^>]+>", "", client.get(url).get_data(as_text=True)))
        assert "A rule you added in the AWS console (expire-media) removes old versions" in body, url
    _live(cfg, [])
    assert "A rule you added in the AWS console" not in client.get("/jobs/new").get_data(as_text=True)


# --- M3: why a set tier is off -----------------------------------------------------------------

def test_a_tier_no_colder_than_the_upload_class_says_so_not_that_s3_removes_first(client, cfg):
    cold_jobs = [dict(JOBS[0], storage_class="DEEP_ARCHIVE"), JOBS[1]]
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": cold_jobs}))
    settings = {"version": 1, "buckets": {BASE: {"folders": {
        "media/manga/": {"tier": {"class": "DEEP_ARCHIVE", "after_days": 30}}}}}}
    lifecycle.save_settings(cfg["CONFIG_DIR"], settings)
    _applied(cfg, jobs=cold_jobs, settings=settings)
    body = html.unescape(client.get("/setup/storage").get_data(as_text=True))
    assert "Off — files here already upload as Deep Archive, so this tier moves nothing." in body
    assert "S3 removes these old versions before they would move" not in body


def test_a_tier_s3_removes_first_keeps_its_own_wording(client, cfg):
    settings = {"version": 1, "buckets": {BASE: {"folders": {
        "media/manga/": {"tier": {"class": "DEEP_ARCHIVE", "after_days": 200}}}}}}
    lifecycle.save_settings(cfg["CONFIG_DIR"], settings)
    _applied(cfg, settings=settings)
    body = html.unescape(client.get("/setup/storage").get_data(as_text=True))
    assert "Off — S3 removes these old versions before they would move." in body
    assert "already upload as" not in body


# --- M4: "permanently removes" only when the preview removes something -----------------------

from datetime import datetime, timezone                                     # noqa: E402

from app.engine import storage_summary                                      # noqa: E402

GIB = 1024 ** 3
GUARD = "This permanently removes backup history."


def _fresh_summary(cfg, folder="media/manga/", *, versions=8):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_age = [[10, 5, 500], [100, 3, 3 * GIB]] if versions else []
    by_rank = [[1, 8, 3 * GIB + 500]] if versions else []
    by_age_rank = [[10, 1, 5, 500], [100, 1, 3, 3 * GIB]] if versions else []
    storage_summary.save(cfg["CACHE_DIR"], {
        "v": 1, "scanned_at": now, "bucket": BASE, "folder": folder,
        "noncurrent_by_age_days": by_age, "noncurrent_by_rank": by_rank, "noncurrent_by_age_rank": by_age_rank,
        "noncurrent_versions": versions, "noncurrent_bytes": (3 * GIB + 500) if versions else 0,
        "delete_markers": 0, "current_objects": 8, "current_bytes": 1})


def _preview(client, **fields):
    data = {"csrf": _csrf(client), "key": f"{BASE}|media/manga/"}
    data.update(fields)
    return html.unescape(client.post("/setup/storage/preview", data=data).get_data(as_text=True))


def test_a_shortening_that_deletes_versions_says_it_permanently_removes_history(client, cfg):
    _applied(cfg)
    _fresh_summary(cfg)
    body = _preview(client, keep="days", days="30")
    assert 'name="typed"' in body and GUARD in body


def test_a_shortening_with_no_summary_still_says_it_may_remove_history(client, cfg):
    _applied(cfg)
    body = _preview(client, keep="days", days="30")
    assert 'name="typed"' in body and GUARD in body


def test_a_tier_only_change_says_what_it_does_not_that_it_removes_history(client, cfg):
    _applied(cfg)
    _fresh_summary(cfg)
    body = _preview(client, keep="days", days="180", tier_class="DEEP_ARCHIVE", tier_days="30")
    assert 'name="typed"' in body
    assert GUARD not in body
    assert "This moves old versions to a cheaper tier" in body


def test_a_suspend_says_what_it_does_not_that_it_removes_history(client, cfg):
    _applied(cfg)
    body = _preview(client, key=f"{BASE}|*", abort_days="7", markers="1", versioning="suspended")
    assert 'name="typed"' in body
    assert GUARD not in body
    assert "This stops S3 keeping old versions in this bucket from now on." in body


# --- M5: the partial-apply flash --------------------------------------------------------------

def _pending(cfg, monkeypatch, err):
    monkeypatch.setattr(lifecycle, "apply_confirmed", lambda *a, **k: (_ for _ in ()).throw(err))


def test_rules_applied_but_versioning_failed_says_so(client, cfg, monkeypatch):
    err = lifecycle.LifecycleError("aws", "AccessDenied")
    err.rules_applied = True
    _pending(cfg, monkeypatch, err)
    token = _csrf(client)
    r = client.post("/setup/storage/apply", data={"csrf": token, "token": "t" * 24, "typed": BASE})
    assert r.status_code in (302, 303)
    text = " ".join(_flashes(client).values())
    assert "S3 rules were applied" in text and "versioning couldn't be changed" in text
    assert "still waits" not in text


def test_a_failed_apply_that_wrote_nothing_still_waits(client, cfg, monkeypatch):
    _pending(cfg, monkeypatch, lifecycle.LifecycleError("aws", "AccessDenied"))
    token = _csrf(client)
    client.post("/setup/storage/apply", data={"csrf": token, "token": "t" * 24, "typed": BASE})
    assert "the change still waits" in " ".join(_flashes(client).values())


def test_the_wizard_confirm_says_so_too(client, cfg, monkeypatch):
    err = lifecycle.LifecycleError("aws", "AccessDenied")
    err.rules_applied = True
    _pending(cfg, monkeypatch, err)
    token = _csrf(client)
    client.post("/jobs/history/confirm", data={"csrf": token, "token": "t" * 24, "typed": BASE, "name": "manga"})
    text = " ".join(_flashes(client).values())
    assert "S3 rules were applied" in text and "versioning couldn't be changed" in text


# --- M6: the cost screens note that "newest N + days" is estimated as newest N ----------------

COMBINED = "keeps the newest old versions plus a number of days as keeping only those newest versions"


def test_cost_screens_note_the_combined_form(client, cfg):
    from tests.gui.test_vocabulary import forbidden_hits, mono_violations
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "count", "count": 5, "days": 30}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    for url in ("/cost", "/jobs/manga"):
        body = client.get(url).get_data(as_text=True)
        assert COMBINED in body, url
        note = re.search(r"<p[^>]*data-combined-note[^>]*>.*?</p>", body, re.S).group(0)
        assert forbidden_hits(note) == [] and mono_violations(note) == []
    assert COMBINED not in client.get("/jobs/appdata_backups").get_data(as_text=True)


def test_no_combined_note_without_the_combined_form(client, cfg):
    assert COMBINED not in client.get("/cost").get_data(as_text=True)


# --- P6: a hand-made job with an unreadable history setting never 500s the job page / Costs ----

def test_the_job_page_and_costs_survive_an_unreadable_history_setting(client, cfg):
    jobs = json.loads(json.dumps(JOBS))
    jobs.append({"name": "odd", "type": "versioned-files", "source": "media/odd", "schedule": "0 3 * * *",
                 "enabled": True, "storage_class": "STANDARD",
                 "retention": {"type": "count", "count": 5, "days": 30}})
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    assert client.get("/jobs/odd").status_code == 200
    assert client.get("/cost").status_code == 200


# --- M7: docs and copy match what the app does now ---------------------------------------------

REPO = Path(__file__).resolve().parents[2]


def _section(text, heading):
    start = text.index(heading)
    nxt = re.search(r"\n#{1,3} ", text[start + len(heading):])
    return text[start: start + len(heading) + (nxt.start() if nxt else len(text))]


def test_the_automated_setup_page_no_longer_says_opentofu_creates_lifecycle_rules(client):
    body = html.unescape(re.sub(r"<[^>]+>", " ", client.get("/setup/destination/automated").get_data(as_text=True)))
    assert "lifecycle rules" not in body
    assert "backup-engine keeps the bucket's S3 rules in step with your jobs" in re.sub(r"\s+", " ", body)


def test_the_readme_describes_how_history_is_kept_now():
    text = re.sub(r"\s+", " ", _section((REPO / "README.md").read_text(), "### How history is kept"))
    for phrase in ("Setup → S3 rules", "preview", "type the bucket name", "before every backup run",
                   "every hour", "storage summary", "cheaper tier", "versioning", "abandoned uploads",
                   "delete markers", "newest N", "keeps the newest N old versions and removes older ones"):
        assert phrase in text, phrase


def test_the_opentofu_readme_mentions_the_hourly_check():
    text = re.sub(r"\s+", " ", (REPO / "opentofu" / "README.md").read_text())
    assert "before every backup run and every hour" in text


# --- M10: before the first check, the overview doesn't claim S3 already does it -----------------

def _row(body, key):
    return re.search(rf'<tr data-row="{re.escape(key)}">.*?</tr>', body, re.S).group(0)


def test_before_the_first_check_the_overview_labels_the_rules_after_the_first_check(client, cfg):
    body = client.get("/setup/storage").get_data(as_text=True)
    row = _row(body, f"{BASE}|media/manga/")
    assert 'Old versions for <span class="mono">180</span> days' in row
    assert "after the first check" in row


def test_before_the_first_apply_the_overview_shows_what_s3_does_now_when_it_was_read(client, cfg):
    legacy = [{"ID": "backstop-media", "Status": "Enabled", "Filter": {"Prefix": "media/"},
               "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}]
    _live(cfg, legacy)
    row = _row(client.get("/setup/storage").get_data(as_text=True), f"{BASE}|media/manga/")
    assert 'Old versions for <span class="mono">30</span> days' in row
    assert "after the first check" not in row


def test_once_applied_the_overview_has_no_first_check_label(client, cfg):
    _applied(cfg)
    row = _row(client.get("/setup/storage").get_data(as_text=True), f"{BASE}|media/manga/")
    assert "after the first check" not in row
