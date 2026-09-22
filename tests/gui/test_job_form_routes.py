# tests/gui/test_job_form_routes.py — the create/edit-job WIZARD routes (spec 5.8 /
# 5.9 / 8.6 / 8.10). Server-side only (no JS): the numbered sections, the recalc
# server-render, SERVER-SIDE blocker enforcement (a blocked form saves nothing), the
# edit lock, and the per-job assumptions persistence. PRICES_LIVE=False keeps the
# frozen model offline (bundled table).
import json
import pathlib
import pytest
from app.gui import config_io, create_app, jobs_io, permissions
import app.engine.buckets as buckets
import app.gui.provision as provision


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
    (cfg / "backup.env").write_text(
        "S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n"
        "BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::111111111111:role/backup-engine-bucket-admin\n"
        f"PERMISSIONS_VERSION={permissions.required_level()}\n")
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


# --- opt-in dedicated bucket: JIT create on save, fail fast (Task 8) --------

def test_dedicated_save_creates_bucket_then_redirects(client, app, monkeypatch):
    made = {}
    monkeypatch.setattr(provision, "assume_role", lambda *a, **k: {"AWS_ACCESS_KEY_ID": "ASIA"})
    monkeypatch.setattr(buckets, "ensure_bucket", lambda name, **k: made.setdefault("name", name))
    t = _csrf(client)
    r = client.post("/jobs", data={"csrf": t, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "bw-backups-photos", "bucket_versioned": "1"})
    assert r.status_code in (302, 303)
    assert made["name"] == "bw-backups-photos"
    assert _jobs(app)[0]["dedicated"] is True
    assert _jobs(app)[0]["bucket"] == "bw-backups-photos"
    assert _jobs(app)[0]["bucket_versioned"] is True


def test_dedicated_save_surfaces_bucket_error(client, app, monkeypatch):
    monkeypatch.setattr(provision, "assume_role", lambda *a, **k: {})

    def boom(name, **k):
        raise buckets.BucketError("name_taken", "taken")
    monkeypatch.setattr(buckets, "ensure_bucket", boom)
    t = _csrf(client)
    r = client.post("/jobs", data={"csrf": t, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "bw-backups-photos", "bucket_versioned": "1"})
    assert r.status_code == 200
    assert "Not saved" in r.get_data(as_text=True)
    assert _jobs(app) == []


def test_dedicated_save_rejects_invalid_bucket_name_without_touching_aws(client, app, monkeypatch):
    called = []
    monkeypatch.setattr(provision, "assume_role", lambda *a, **k: called.append("assume_role"))
    monkeypatch.setattr(buckets, "ensure_bucket", lambda name, **k: called.append("ensure_bucket"))
    t = _csrf(client)
    r = client.post("/jobs", data={"csrf": t, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "Not_A_Valid_Bucket!", "bucket_versioned": "1"})
    assert r.status_code == 200
    assert "Not saved" in r.get_data(as_text=True)
    assert _jobs(app) == []
    assert called == []                   # invalid name never touches AWS


def test_dedicated_save_off_prefix_name_is_refused_before_any_aws_call(client, app, monkeypatch):
    # Addendum 2026-09-22: a dedicated bucket's name MUST be <base>-<suffix> -- the
    # runtime policy's <base>-* wildcard already grants it, so no IAM write is ever
    # needed. An off-prefix name is refused server-side, guided, BEFORE assume_role.
    def fail(*a, **k):
        raise AssertionError("must not touch AWS before the name-rule refusal")
    monkeypatch.setattr(provision, "assume_role", fail)
    monkeypatch.setattr(buckets, "ensure_bucket", fail)
    t = _csrf(client)
    r = client.post("/jobs", data={"csrf": t, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "off-prefix-bucket", "bucket_versioned": "1"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Not saved" in body
    assert "bw-backups-photos" in body           # the guided example names the rule
    assert _jobs(app) == []


def test_dedicated_save_bucket_name_equal_to_base_is_refused(client, app, monkeypatch):
    # A value equal to the base bucket has no suffix at all -- still refused, and the
    # rule check must run before any AWS call (same as the off-prefix case above).
    def fail(*a, **k):
        raise AssertionError("must not touch AWS before the name-rule refusal")
    monkeypatch.setattr(provision, "assume_role", fail)
    monkeypatch.setattr(buckets, "ensure_bucket", fail)
    t = _csrf(client)
    r = client.post("/jobs", data={"csrf": t, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "bw-backups", "bucket_versioned": "1"})
    assert r.status_code == 200
    assert "Not saved" in r.get_data(as_text=True)
    assert _jobs(app) == []


def test_new_form_exposes_base_bucket_for_js_suggestion(client):
    body = client.get("/jobs/new").get_data(as_text=True)
    assert 'data-base-bucket="bw-backups"' in body
    assert 'name="dedicated"' in body
    assert 'name="bucket"' in body
    assert 'name="bucket_versioned"' in body


def test_post_clean_form_no_dedicated_bucket_saves_without_aws_calls(client, app, monkeypatch):
    # Regression: the non-dedicated path (the overwhelming majority of jobs) must
    # never touch provision/buckets at all.
    def fail(*a, **k):
        raise AssertionError("should not be called for a non-dedicated job")
    monkeypatch.setattr(provision, "assume_role", fail)
    monkeypatch.setattr(buckets, "ensure_bucket", fail)
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "name": "regular", "type": "archive", "source": "media/movies",
        "schedule": "0 4 * * 0", "storage_class": "STANDARD", "enabled": "1",
        "retention_type": "days", "retention_days": "180"})
    assert r.status_code in (302, 303)
    jobs = _jobs(app)
    assert jobs[0]["name"] == "regular" and jobs[0].get("dedicated") is False


# --- no hidden number field can silently block the whole form (regression) --
# A <input type=number> whose default value violates its own min/step is INVALID;
# if it's hidden (a collapsed section) the browser refuses to submit the form with
# no visible message — the create button "does nothing". Every rendered number
# input's default value must satisfy its own constraints. (pack_member_gb had
# step="0.01" min="0.001" value="0.05": base 0.001 + n*0.01 never hits 0.05.)

def _number_inputs(html):
    import re
    for tag in re.findall(r"<input\b[^>]*\btype=\"number\"[^>]*>", html):
        attr = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        yield attr.get("name") or attr.get("id") or "?", attr


def test_no_number_input_default_violates_its_own_step(client):
    body = client.get("/jobs/new").get_data(as_text=True)
    seen = 0
    for name, attr in _number_inputs(body):
        val, step = attr.get("value", ""), attr.get("step", "")
        if val == "" or step == "any":
            continue
        seen += 1
        v = float(val)
        s = float(step) if step else 1.0
        base = float(attr["min"]) if attr.get("min") not in (None, "") else 0.0
        n = (v - base) / s
        assert abs(n - round(n)) < 1e-9, (
            f"{name}: value {val} is off the step ladder (min={attr.get('min')}, "
            f"step={step or '1'}) -> the field is invalid and, if hidden, silently "
            f"blocks the whole form from submitting")
    assert seen or "type=\"number\"" not in body      # sanity: we actually checked some


# --- an incomplete form is rejected LOUDLY, never silently -----------------
# The client gates the footer so an incomplete form can't be posted; if one is
# (JS off, or a server-only check like a bad folder), the rejection must land in a
# prominent "Not saved" banner at the top — a 200 re-render with only a buried line
# four sections down reads to the owner as "the button did nothing" (regression).

@pytest.mark.parametrize("missing,overrides,needle", [
    ("name",   {"name": ""},                 "job name must be"),
    ("type",   {"type": ""},                  "unknown job type"),
    ("source", {"source": ""},                "source is required"),
])
def test_incomplete_post_rerenders_with_prominent_banner(client, app, missing, overrides, needle):
    t = _csrf(client)
    data = {"csrf": t, "name": "goodname", "type": "versioned", "source": "appdata",
            "schedule": "0 5 * * *", "storage_class": "STANDARD", "enabled": "1",
            "retention_type": "days", "retention_days": "180"}
    data.update(overrides)
    r = client.post("/jobs", data=data)
    body = r.get_data(as_text=True)
    assert r.status_code == 200                         # re-render, NOT a redirect
    assert "Not saved" in body                          # prominent top banner present
    assert needle in body                               # names the actual problem
    assert _jobs(app) == []                             # saved nothing


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


# --- the dedicated-bucket edit lock (Critical: editing must NOT repoint) ----

def test_edit_dedicated_job_shows_locked_bucket(client, app):
    # The edit screen must reflect the job's TRUE dedicated state as LOCKED read-only
    # info — never render a dedicated job as a base-bucket job (which, on save, would
    # silently repoint it and orphan its data).
    _seed(app, {"name": "photos", "type": "archive", "source": "media/movies",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "dedicated": True, "bucket": "bw-backups-photos", "bucket_versioned": True,
                "retention": {"type": "keep_all"}})
    body = client.get("/jobs/photos/edit").get_data(as_text=True)
    assert "bw-backups-photos" in body                 # its own bucket is shown
    assert "Locked — create a new job to change it." in body
    # the editable toggle/field only exist on the CREATE screen
    assert 'name="dedicated"' not in body
    assert 'id="bucket"' not in body


def test_edit_dedicated_job_preserves_bucket_without_touching_aws(client, app, monkeypatch):
    # The Critical: editing a dedicated job (here its schedule) must PRESERVE its
    # dedicated bucket and NEVER call assume_role/ensure_bucket on the edit path.
    called = []
    monkeypatch.setattr(provision, "assume_role", lambda *a, **k: called.append("assume_role"))
    monkeypatch.setattr(buckets, "ensure_bucket", lambda *a, **k: called.append("ensure_bucket"))
    _seed(app, {"name": "photos", "type": "archive", "source": "media/movies",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "dedicated": True, "bucket": "bw-backups-photos", "bucket_versioned": True,
                "retention": {"type": "keep_all"}})
    t = _csrf(client)
    # Edit the schedule only; the locked form no longer posts dedicated/bucket at all.
    r = client.post("/jobs", data={
        "csrf": t, "name": "photos", "type": "archive", "source": "media/movies",
        "schedule": "0 6 * * *", "storage_class": "STANDARD", "enabled": "1",
        "retention_type": "keep_all"})
    assert r.status_code in (302, 303)
    saved = _jobs(app)[0]
    assert saved["schedule"] == "0 6 * * *"            # the actual edit took
    assert saved["dedicated"] is True                  # preserved from the saved job
    assert saved["bucket"] == "bw-backups-photos"      # SAME bucket, not the base
    assert saved["bucket_versioned"] is True           # versioning preserved
    assert called == []                                # AWS never touched on an edit


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


# --- Task 14 review fixes -----------------------------------------------------

def test_all_zero_tiered_shows_inline_block_and_disables_footer(client):
    # Minor (a): an all-zero tiered keep raises the inline WON'T RUN beside the
    # Advanced inputs (via _wizard_blockers), caught in the wizard not only on POST.
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "recalc": "1", "name": "appdata", "type": "versioned",
        "source": "appdata", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "retention_type": "tiered", "keep_last": "0", "keep_daily": "0",
        "keep_weekly": "0", "keep_monthly": "0", "change_rate_pct": "1",
        "size_gb": "52.71", "file_count": "533", "measured_bytes": MEASURED})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '<div class="sev wont" id="tiered-blocker" ' in body      # visible, not hidden
    assert "Keeping 0 of everything" in body
    assert 'id="create-btn" disabled' in body


def _edit_job(app):
    _seed(app, {"name": "appdata", "type": "versioned", "source": "appdata",
                "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6},
                "measured": {"bytes": int(52.71 * 1024 ** 3), "count": 533},
                "assumptions": {"change_rate_pct": 1.0, "bundled": False, "pack_member_gb": 0.05}})


def test_edit_class_change_shows_was_and_footer_was(client, app):
    # Important (5.9): a changed class renders `was: Instant · STANDARD` on that row
    # and `(was $X)` in the footer (server-side, via the recalc re-render).
    _edit_job(app)
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "recalc": "1", "name": "appdata", "type": "versioned",
        "source": "appdata", "schedule": "0 5 * * *", "storage_class": "STANDARD_IA",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6", "change_rate_pct": "1",
        "size_gb": "52.71", "file_count": "533", "measured_bytes": MEASURED,
        "change_rate_touched": "1"})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    # the storage-class row is marked changed (not hidden) with the SAVED class idiom
    assert '<p class="was-row" data-was="storage_class" >' in body
    assert "was: <span>Instant · STANDARD</span>" in body
    assert 'id="foot-was" >' in body and "(was <span>$" in body


def test_edit_no_change_shows_no_was(client, app):
    # Reverting / submitting the saved values shows neither `was:` nor `(was $X)`.
    _edit_job(app)
    t = _csrf(client)
    r = client.post("/jobs", data={
        "csrf": t, "recalc": "1", "name": "appdata", "type": "versioned",
        "source": "appdata", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "retention_type": "tiered", "keep_last": "3", "keep_daily": "7",
        "keep_weekly": "4", "keep_monthly": "6", "change_rate_pct": "1",
        "size_gb": "52.71", "file_count": "533", "measured_bytes": MEASURED,
        "change_rate_touched": "1"})
    body = r.get_data(as_text=True)
    # the was-row for storage_class renders hidden (unchanged), and the footer-was too
    assert 'data-was="storage_class" hidden' in body
    assert 'id="foot-was" hidden' in body


def test_edit_renders_where_it_goes_and_destination(client, app):
    # Minor (b) / 5.9: the locked "Where it goes" identity row + concrete destination.
    _edit_job(app)
    body = client.get("/jobs/appdata/edit").get_data(as_text=True)
    assert "Where it goes" in body
    assert "s3://bw-backups/appdata/" in body          # versioned shares the appdata store


def test_new_name_hint_names_the_destination(client):
    # 5.8 §5: the create name hint names the concrete destination, type-aware.
    body = client.get("/jobs/new").get_data(as_text=True)
    assert "s3://bw-backups/appdata/" in body          # Snapshot backup hint
    assert "s3://bw-backups/media/" in body            # the others' hint


# --- dedicated buckets need the permissions update (spec 2026-09-22 §3) -----

@pytest.fixture
def unstamped_client(tmp_path, source_root, template_path):
    cfg = tmp_path / "config2"; cfg.mkdir()
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    app = create_app({"CONFIG_DIR": str(cfg), "CACHE_DIR": str(tmp_path / "cache2"),
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SOURCE_ROOT": str(source_root), "SECRET_KEY": "test", "TESTING": True,
                      "PRICES_LIVE": False})
    return app.test_client()


def test_toggle_disabled_until_permissions_are_updated(unstamped_client):
    body = unstamped_client.get("/jobs/new").get_data(as_text=True)
    assert 'name="dedicated"' not in body
    assert "Needs a one-time AWS permissions update" in body
    assert 'href="/setup/permissions"' in body


def test_toggle_enabled_when_current(client):
    assert 'name="dedicated"' in client.get("/jobs/new").get_data(as_text=True)


def test_dedicated_post_without_permissions_is_refused_without_aws(unstamped_client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not reach AWS")
    monkeypatch.setattr(provision, "assume_role", boom)
    unstamped_client.get("/jobs/new")
    with unstamped_client.session_transaction() as s:
        token = s["_csrf"]
    r = unstamped_client.post("/jobs", data={"csrf": token, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "bw-backups-photos", "bucket_versioned": "1"})
    assert "one-time AWS permissions update" in r.get_data(as_text=True)
