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
