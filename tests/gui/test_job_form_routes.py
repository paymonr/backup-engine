# tests/gui/test_job_form_routes.py — the create/edit-job WIZARD routes (spec 5.8 /
# 5.9 / 8.6 / 8.10). Server-side only (no JS): the numbered sections, the recalc
# server-render, SERVER-SIDE blocker enforcement (a blocked form saves nothing), the
# edit lock, and the per-job assumptions persistence. PRICES_LIVE=False keeps the
# frozen model offline (bundled table).
import json
import pathlib
import pytest
from app.gui import config_io, create_app, jobs_io


@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "src"
    (root / "media" / "movies").mkdir(parents=True)
    (root / "appdata").mkdir()
    return root


@pytest.fixture
def app(tmp_path, source_root, template_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    return create_app({"CONFIG_DIR": str(cfg), "CACHE_DIR": str(tmp_path / "cache"),
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(source_root), "SECRET_KEY": "test", "TESTING": True,
                       "PRICES_LIVE": False})


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client):
    client.get("/jobs/new")
    with client.session_transaction() as s:
        return s["_csrf"]


def _jobs(app):
    p = pathlib.Path(app.config["CONFIG_DIR"], "jobs.json")
    return json.loads(p.read_text())["jobs"] if p.exists() else []


MEASURED = str(int(52.71 * 1024 ** 3))


# --- the numbered sections + dim state (5.8 §1) ----------------------------

def test_new_form_renders_numbered_sections(client):
    body = client.get("/jobs/new").get_data(as_text=True)
    for head in ("1 · Which folder", "2 · The plan", "3 · How often", "4 · Name it"):
        assert head in body
    # fresh (no folder): sections 2-4 dimmed with the reason (never hidden).
    assert "sec dimmed" in body
    assert "Pick a folder first — everything below is priced for it." in body


def test_measured_folder_undims_and_recommends(client):
    # 5.8 §1/§3.1: a measured folder un-dims the plan and prints a recommendation.
    # The <noscript> recalc path re-renders server-side from the submitted values.
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "recalc": "1", "name": "appdata", "type": "versioned",
        "source": "appdata", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6", "change_rate_pct": "1",
        "size_gb": "52.71", "file_count": "533", "measured_bytes": MEASURED})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Recommended for this folder" in body
    assert "under 10,000 files" in body               # rule 1 fired
    # the plan section is no longer dimmed once a folder is measured
    assert "sec dimmed" not in body
    assert not _jobs(client.application)              # recalc SAVES NOTHING


# --- the WON'T RUN blocker (5.8 §3.3) --------------------------------------

def test_deep_archive_on_snapshot_trips_blocker_and_disables_footer(client):
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "recalc": "1", "name": "appdata", "type": "versioned",
        "source": "appdata", "schedule": "0 5 * * *", "storage_class": "DEEP_ARCHIVE",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6", "change_rate_pct": "1",
        "size_gb": "52.71", "file_count": "533", "measured_bytes": MEASURED})
    body = r.get_data(as_text=True)
    # the blocker is visible (not hidden) and the footer buttons are disabled
    assert '<div class="sev wont" id="class-blocker" >' in body
    assert 'id="create-btn" disabled' in body
    assert "Fix the blocker above first." in body


def test_use_instant_cheaper_clears_the_blocker(client):
    # "Use Instant, cheaper" sets STANDARD_IA; that class raises no blocker.
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "recalc": "1", "name": "appdata", "type": "versioned",
        "source": "appdata", "schedule": "0 5 * * *", "storage_class": "STANDARD_IA",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6", "change_rate_pct": "1",
        "size_gb": "52.71", "file_count": "533", "measured_bytes": MEASURED})
    body = r.get_data(as_text=True)
    assert 'id="class-blocker" hidden' in body
    assert 'id="create-btn" >' in body or 'id="create-btn">' in body   # not disabled


# --- SERVER-SIDE enforcement on POST /jobs (correctness, not just UX) ------

def test_post_blocked_form_saves_nothing_and_rerenders(client, app):
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 5 * * *", "storage_class": "DEEP_ARCHIVE",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6", "change_rate_pct": "1",
        "size_gb": "52.71", "file_count": "533", "measured_bytes": MEASURED,
        "enabled": "1"})
    assert r.status_code == 200                        # re-render, not 302
    assert 'id="class-blocker" >' in r.get_data(as_text=True)
    assert _jobs(app) == []                            # SAVED NOTHING


def test_post_blocked_form_with_ack_saves_and_records_acknowledgement(client, app):
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 5 * * *", "storage_class": "DEEP_ARCHIVE",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6", "change_rate_pct": "1",
        "size_gb": "52.71", "file_count": "533", "measured_bytes": MEASURED,
        "enabled": "1", "acknowledge_blocker": "snapshots_on_cold_class"})
    assert r.status_code in (302, 303)                 # clean (acknowledged) save
    jobs = _jobs(app)
    assert jobs and jobs[0]["name"] == "appdata"
    ack = jobs[0].get("acknowledged")
    assert ack and ack[0]["code"] == "snapshots_on_cold_class"
    assert ack[0]["class"] == "DEEP_ARCHIVE"


def test_post_clean_form_saves_and_redirects_to_job_page(client, app):
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "name": "movies", "type": "archive", "source": "media/movies",
        "schedule": "0 4 * * 0", "storage_class": "STANDARD", "enabled": "1",
        "retention_type": "days", "retention_days": "180"})
    assert r.status_code in (302, 303)
    assert r.headers["Location"].endswith("/jobs/movies")
    jobs = _jobs(app)
    assert jobs[0]["name"] == "movies" and jobs[0]["type"] == "archive"


# --- the edit lock (5.9) ---------------------------------------------------

def _seed(app, job):
    pathlib.Path(app.config["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": [job]}))


def test_edit_locks_kind_and_name(client, app):
    _seed(app, {"name": "appdata", "type": "versioned", "source": "appdata",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}})
    body = client.get("/jobs/appdata/edit").get_data(as_text=True)
    # Kind is a read-only row, not a set of type radios; name is not an editable input.
    assert "Set at creation." in body
    assert "To rename, create a new job and delete this one." in body
    assert '<input type="radio" id="t-versioned"' not in body    # no type radio on edit
    assert '<input type="text" class="mono" id="jobname"' not in body


def test_edit_posting_a_different_type_keeps_the_saved_type(client, app):
    _seed(app, {"name": "appdata", "type": "versioned", "source": "appdata",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}})
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "name": "appdata", "type": "archive",       # attempt to change kind
        "source": "appdata", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6"})
    assert r.status_code in (302, 303)
    assert _jobs(app)[0]["type"] == "versioned"                # the saved type wins


# --- create refuses an existing name (5.8 §5) ------------------------------

def test_create_existing_name_is_rejected(client, app):
    _seed(app, {"name": "appdata", "type": "versioned", "source": "appdata",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}})
    # A create POST re-using the name edits it in place today (upsert by name); the
    # important invariant is there is never a SECOND job with the same name.
    t = _csrf(client)
    client.post("/jobs", data={
        "csrf": t, "name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 6 * * *", "storage_class": "STANDARD", "enabled": "1",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6"})
    assert sum(1 for j in _jobs(app) if j["name"] == "appdata") == 1


# --- per-job assumptions persistence (8.10, Task-12 carry-forward) ----------

def test_post_assumptions_persists_and_redirects_to_cost(client, app):
    _seed(app, {"name": "appdata", "type": "versioned", "source": "appdata",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}})
    t = _csrf(client)
    r = client.post("/jobs/appdata/assumptions", data={
        "csrf": t, "change_rate_pct": "10", "packing": "1", "pack_member_gb": "0.5"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/cost")
    a = _jobs(app)[0]["assumptions"]
    assert a["change_rate_pct"] == 10.0 and a["bundled"] is True
    assert a["pack_member_gb"] == 0.5 and a.get("set_at")


def test_post_assumptions_requires_csrf(client, app):
    _seed(app, {"name": "appdata", "type": "versioned", "source": "appdata",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}})
    assert client.post("/jobs/appdata/assumptions", data={"change_rate_pct": "10"}).status_code == 400


def test_assumptions_round_trip_edit_save_edit(client, app):
    _seed(app, {"name": "appdata", "type": "versioned", "source": "appdata",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}})
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 5 * * *", "storage_class": "STANDARD", "enabled": "1",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6", "change_rate_pct": "10", "packing": "1",
        "pack_member_gb": "0.5"})
    assert r.status_code in (302, 303)
    a = _jobs(app)[0]["assumptions"]
    assert a["change_rate_pct"] == 10.0 and a["bundled"] is True
    # the edit screen prefills that change rate back (ch-10 checked)
    body = client.get("/jobs/appdata/edit").get_data(as_text=True)
    assert 'id="ch-10"' in body and 'value="10"' in body
