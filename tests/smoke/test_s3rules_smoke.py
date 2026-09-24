# tests/smoke/test_s3rules_smoke.py — proves the one-shot real-AWS smoke test
# (tests/smoke/s3rules_smoke.py) end to end against an in-memory fake aws CLI (fake_aws.py): every
# check PASSes, the scratch buckets are always deleted (a failing check, Ctrl-C and a safety-guard
# abort included), the live buckets only ever see the three read-only calls, and no secret reaches
# the report, the snapshot or the terminal. Never touches AWS.
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fake_aws import FakeAWS  # noqa: E402
from app.engine import lifecycle  # noqa: E402

_spec = importlib.util.spec_from_file_location("s3rules_smoke", HERE / "s3rules_smoke.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

LIVE = "unraid-backup-123456789012"
LIVE_PHOTOS = f"{LIVE}-photos"
KEY, SECRET, TOKEN = "AKIASMOKETESTKEY0001", "smokeSECRET/value+abc123xyz", "smokeTOKENvalue//0987zyx=="
CREDS = {"AWS_ACCESS_KEY_ID": KEY, "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_SESSION_TOKEN": TOKEN}
LIVE_LEGACY = [dict(r) for r in smoke.LEGACY_BASE_RULES]
ALL_CHECKS = ["S1", "S2", "SETUP", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13"]


@pytest.fixture
def live_config(tmp_path):
    d = tmp_path / "liveconfig"
    d.mkdir()
    (d / "backup.env").write_text(f"S3_BUCKET={LIVE}\nAWS_REGION=eu-west-2\nPERMISSIONS_VERSION=3\n"
                                  "BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::123456789012:role/backup-engine-bucket-admin\n")
    (d / "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *"},
        {"name": "photos", "type": "archive", "source": "media/photos", "schedule": "0 3 * * *",
         "dedicated": True, "bucket": LIVE_PHOTOS, "bucket_versioned": True}]}))
    return d


def _fake(region="eu-west-2", creds=CREDS, **kw):
    return FakeAWS(region=region, creds=creds, live={
        LIVE: {"versioning": "Enabled", "lifecycle": LIVE_LEGACY, "region": region},
        LIVE_PHOTOS: {"versioning": "Enabled", "lifecycle": None, "region": region}}, **kw)


class FakeTofu:
    def __init__(self, missing=False):
        self.calls, self.missing = [], missing

    def __call__(self, args, *, cwd=None, timeout=120):
        self.calls.append((list(args), cwd))
        if self.missing:
            raise FileNotFoundError("tofu")
        if args[0] == "init":
            assert Path(cwd, "main.tf").is_file() and Path(cwd, "..", "provisioning", "permissions.json").is_file()
            assert not list(Path(cwd).glob("*.tfstate*")), "state must never be copied"
        return SimpleNamespace(returncode=0, stdout="OpenTofu v1.8.5\non linux_amd64\n", stderr="")


def _run(tmp_path, live_config, fake, *, env=None, tofu=None):
    out = io.StringIO()
    s = smoke.execute(run=fake, tofu_run=tofu or FakeTofu(), env=dict(CREDS if env is None else env),
                      out_dir=tmp_path / "out", live_config=live_config, app_root=REPO,
                      sleep=lambda s: None, out=out)
    return s, out.getvalue()


def _statuses(s):
    return {o.cid: o.status for o in s.results}


def _no_secrets(*texts):
    for t in texts:
        for v in (KEY, SECRET, TOKEN):
            assert v not in t


def _scratch_gone(s, fake):
    assert s.created and set(s.created) == {s.base, s.ded, s.nv}
    assert set(fake.deleted) >= set(s.created)
    assert not [b for b in fake.buckets if "-s3smoke-" in b]
    assert s.leftovers == []


# --- the whole flow -------------------------------------------------------------------------------

def test_every_check_passes_against_the_fake_and_cleanup_deletes_every_scratch_bucket(tmp_path, live_config):
    fake = _fake()
    s, printed = _run(tmp_path, live_config, fake)
    st = _statuses(s)
    assert list(st) == ALL_CHECKS
    fails = {o.cid: o.fails for o in s.results if o.status == "FAIL"}
    assert fails == {}
    assert all(st[c] == "PASS" for c in ALL_CHECKS if c not in ("S11", "S12")), st
    assert st["S11"] == "SKIP" and st["S12"] == "SKIP"
    assert s.exit_code == 0 and s.overall() == "PASS"
    _scratch_gone(s, fake)
    # the live buckets: read-only calls only, and exactly as they were
    assert fake.live_mutations == [] and fake.live_unchanged()
    live_ops = {c["op"] for c in fake.calls if c["bucket"] in (LIVE, LIVE_PHOTOS)}
    assert live_ops == {"get-bucket-lifecycle-configuration", "get-bucket-versioning", "get-bucket-location"}
    assert fake.cred_errors == [] and fake.unknown == []
    assert s.guard.refused == []
    # the report and the snapshot
    report = s.report_path.read_text()
    snap = json.loads(s.snapshot_path.read_text())
    _no_secrets(report, s.snapshot_path.read_text(), printed)
    assert "OVERALL   PASS" in report and "LEFTOVER" not in report
    assert snap["buckets"][LIVE]["lifecycle"]["Rules"][0]["ID"] == "backstop-appdata"
    assert snap["buckets"][LIVE_PHOTOS]["lifecycle"] == {"Rules": []}
    assert snap["buckets"][LIVE_PHOTOS]["role"] == "dedicated (photos)"
    assert "aws s3api put-bucket-lifecycle-configuration --bucket " + s.base in report
    for cid in ALL_CHECKS:
        assert f"\n{cid:<6}" in printed
    # the scratch install's temp dir is gone; the app's module-level seams are back
    assert not s.workdir.exists()
    assert lifecycle.role_creds.__name__ == "role_creds"


def test_the_flow_really_exercises_the_app(tmp_path, live_config):
    """Not just PASS: the fake saw what each check is about."""
    fake = _fake()
    s, _ = _run(tmp_path, live_config, fake)
    puts = [json.loads(c["args"][c["args"].index("--lifecycle-configuration") + 1])["Rules"]
            for c in fake.ops("put-bucket-lifecycle-configuration") if "--lifecycle-configuration" in c["args"]]
    flat = [r for rules in puts for r in rules]
    assert any(r.get("NoncurrentVersionTransitions") == [{"NoncurrentDays": 179, "StorageClass": "DEEP_ARCHIVE"}]
               for r in flat)
    assert any(r.get("NoncurrentVersionExpiration") == {"NoncurrentDays": 36500} for r in flat)
    assert any(r.get("ID") == "backup-engine" and r.get("Filter") == {} for r in flat)       # 66dadf1's shape
    vers = [c["args"] for c in fake.ops("put-bucket-versioning")]
    assert any("Status=Suspended" in a and s.base in a for a in vers)
    assert any("Status=Suspended" in a and s.nv in a for a in vers)
    pages = [c for c in fake.ops("list-object-versions")
             if json.loads(c["args"][c["args"].index("--cli-input-json") + 1]).get("MaxKeys") == 2]
    assert len(pages) >= 8                         # the op's scan + the aged scan, 2 per page
    probes = [c for c in fake.calls if "--generate-cli-skeleton" in c["args"]]
    assert probes and all(c["anon"] and c["token"] is None for c in probes)
    assert not [c for c in fake.calls if "help" in c["args"]]              # Alpine's CLI has no help docs
    create = [c["args"] for c in fake.ops("create-bucket")]
    assert all("LocationConstraint=eu-west-2" in a for a in create)
    s10 = next(o for o in s.results if o.cid == "S10")
    assert "impact 3 days on: 1-day rule 4, newest-1 rule 1" in s10.summary


def test_us_east_1_creates_buckets_without_a_location_constraint_and_no_session_token(tmp_path, live_config):
    env = live_config / "backup.env"
    env.write_text(env.read_text().replace("eu-west-2", "us-east-1"))
    creds = {k: v for k, v in CREDS.items() if k != "AWS_SESSION_TOKEN"}
    fake = _fake(region="us-east-1", creds=creds)
    s, _ = _run(tmp_path, live_config, fake, env=creds)
    assert s.exit_code == 0, {o.cid: o.fails for o in s.results if o.fails}
    assert all("--create-bucket-configuration" not in c["args"] for c in fake.ops("create-bucket"))
    _scratch_gone(s, fake)


def test_lagging_s3_reads_are_waited_out(tmp_path, live_config):
    fake = _fake(lag_reads=2)
    s, _ = _run(tmp_path, live_config, fake)
    assert s.exit_code == 0, {o.cid: o.fails for o in s.results if o.fails}
    assert any(n > smoke.SETTLE_AGREE for _, _, _, n, _ in s.settles)
    assert f"needed more than {smoke.SETTLE_AGREE} reads" in s.report_path.read_text()
    _scratch_gone(s, fake)


def test_s3_answering_new_then_old_again_never_fails_a_check(tmp_path, live_config):
    """Smoke test run 1 (S7): right after a put S3 answered one read with the new configuration and
    the next with the OLD one. The settle loop wants SETTLE_AGREE reads in a row, every verification
    read goes through it, and S13 catches the app's own check reading the old rules."""
    fake = _fake(stale_pattern="FS")
    s, _ = _run(tmp_path, live_config, fake)
    assert s.exit_code == 0, {o.cid: o.fails for o in s.results if o.fails}
    assert fake.stale_served > 0
    s13 = next(o for o in s.results if o.cid == "S13")
    assert s13.status == "PASS" and "read the rules from before the job save" in s13.summary
    assert "ok, no alarm, nothing written" in s13.summary
    _scratch_gone(s, fake)


def test_s13_is_informational_when_s3_serves_no_stale_read(tmp_path, live_config):
    s, _ = _run(tmp_path, live_config, _fake())
    s13 = next(o for o in s.results if o.cid == "S13")
    assert s13.status == "PASS" and "no stale read occurred" in s13.summary and "(informational)" in s13.summary


@pytest.mark.parametrize("step", ["tamper", "console"])
def test_s7_re_runs_a_check_that_s3_answered_from_before_the_outside_edit(tmp_path, live_config, monkeypatch, step):
    """The check S7 is about must see the outside edit; one that S3 answered from before it had
    nothing to find (ok, nothing written) -- S7 notes it and re-runs it instead of failing."""
    real, fired = smoke.Smoke.put_lifecycle, []

    def is_step(rules):
        if step == "console":
            return any(r.get("ID") == "smoke-console" for r in rules)
        return any((r.get("NoncurrentVersionExpiration") or {}).get("NoncurrentDays") == 1 for r in rules)

    def put_then_serve_stale(self, bucket, rules):
        real(self, bucket, rules)
        if self.rec.label == "S7" and is_step(rules) and not fired:
            fired.append(bucket)                   # settled SETTLE_AGREE reads in a row -- then old again
            fake.serve_stale(bucket, "lifecycle", "F" * smoke.SETTLE_AGREE + "S")
    monkeypatch.setattr(smoke.Smoke, "put_lifecycle", put_then_serve_stale)
    fake = _fake()
    s, _ = _run(tmp_path, live_config, fake)
    s7 = next(o for o in s.results if o.cid == "S7")
    assert fired and fake.stale_served == 1
    assert s7.status == "PASS", s7.fails
    assert any("a stale read" in n for n in s7.notes)
    _scratch_gone(s, fake)


def test_a_cli_that_knows_the_min_size_flag_is_reported(tmp_path, live_config):
    # the fake's `help` fails like Alpine's (no docs) -- the skeleton probe still sees the flag
    fake = _fake(cli_has_flag=True)
    s, _ = _run(tmp_path, live_config, fake)
    assert s.exit_code == 0
    s9 = next(o for o in s.results if o.cid == "S9")
    assert "IS supported" in s9.summary


def test_a_cli_without_the_min_size_flag_is_reported(tmp_path, live_config):
    s, _ = _run(tmp_path, live_config, _fake())
    s9 = next(o for o in s.results if o.cid == "S9")
    assert s9.status == "PASS" and "is NOT supported" in s9.summary and "skeleton rc=0" in s9.summary


# --- failures, interrupts, the guard: cleanup always runs ------------------------------------------

def test_s3_answering_in_a_form_the_app_does_not_normalize_fails_the_zero_put_checks(tmp_path, live_config):
    """The zero-put re-checks aren't vacuous: if S3 handed the rules back in a form lifecycle._norm
    doesn't treat as equal (here: days as strings), every check rewrites -- S3/S4 FAIL, saying how
    S3's copy differs, and the run still finishes and cleans up."""
    def as_strings(rule):
        nce = rule.get("NoncurrentVersionExpiration")
        if nce and rule.get("ID", "").startswith("backup-engine:"):
            nce["NoncurrentDays"] = str(nce["NoncurrentDays"])
        return rule
    fake = _fake(mangle_get=as_strings)
    s, _ = _run(tmp_path, live_config, fake)
    by = {o.cid: o for o in s.results}
    assert by["S3"].status == "FAIL" and by["S4"].status == "FAIL"
    assert any("wrote again" in f for f in by["S3"].fails)
    assert any("S3 returned" in n and '"NoncurrentDays": "180"' in n for n in by["S3"].notes)
    assert s.exit_code == 1
    _scratch_gone(s, fake)


def test_a_check_failing_midway_still_runs_the_rest_and_cleans_up(tmp_path, live_config):
    def fail(op, bucket, args):
        if op == "put-bucket-versioning" and "Status=Suspended" in args and bucket and \
                "-s3smoke-" in bucket and not bucket.endswith(("-ded", "-nv")):
            return "AccessDenied", f"not allowed for {KEY} / {SECRET}"      # a secret in an error: scrubbed
    fake = _fake(fail=fail)
    s, printed = _run(tmp_path, live_config, fake)
    st = _statuses(s)
    assert st["S6"] == "FAIL" and list(st) == ALL_CHECKS
    assert st["S7"] in ("PASS", "FAIL") and st["S8"] == "PASS" and st["S10"] in ("PASS", "FAIL")
    assert s.exit_code == 1 and s.overall().startswith("FAIL")
    _scratch_gone(s, fake)
    report = s.report_path.read_text()
    _no_secrets(report, printed)
    assert "[REDACTED]" in report and "AccessDenied" in report


def test_s3_refusing_a_tier_fails_s5_only_the_later_checks_start_clean(tmp_path, live_config):
    def refuse_glacier_ir(op, bucket, args):
        if op == "put-bucket-lifecycle-configuration" and "GLACIER_IR" in " ".join(args):
            return "InvalidArgument", "no Glacier IR here"
    fake = _fake(fail=refuse_glacier_ir)
    s, _ = _run(tmp_path, live_config, fake)
    st = _statuses(s)
    assert st["S5"] == "FAIL" and "no Glacier IR here" in next(o for o in s.results if o.cid == "S5").fails[0]
    assert all(st[c] == "PASS" for c in ("S6", "S7", "S8", "S10")), st
    assert any("storage.json put back" in n for n in next(o for o in s.results if o.cid == "S5").notes)
    _scratch_gone(s, fake)


def test_s3_refusing_one_rule_shape_names_it_and_the_later_checks_start_clean(tmp_path, live_config):
    def refuse_36500(op, bucket, args):
        if op == "put-bucket-lifecycle-configuration" and '"NoncurrentDays": 36500' in " ".join(args):
            return "InvalidArgument", "NoncurrentDays too large"
    fake = _fake(fail=refuse_36500)
    s, _ = _run(tmp_path, live_config, fake)
    st = _statuses(s)
    s4 = next(o for o in s.results if o.cid == "S4")
    assert st["S4"] == "FAIL" and any("S3 refused smk-d36500" in f and "too large" in f for f in s4.fails)
    assert not any("smk-n10 " in f for f in s4.fails)
    assert all(st[c] == "PASS" for c in ("S5", "S6", "S7", "S8", "S10")), st
    _scratch_gone(s, fake)


def test_ctrl_c_midway_still_cleans_up(tmp_path, live_config):
    fake = _fake(interrupt=lambda op, bucket, args: op == "put-object")       # S10's first write
    s, printed = _run(tmp_path, live_config, fake)
    st = _statuses(s)
    assert st["S10"] == "FAIL" and st["S11"] == "SKIP" and st["S12"] == "SKIP"
    assert s.interrupted and s.exit_code == 130
    _scratch_gone(s, fake)
    assert "INTERRUPTED" in s.report_path.read_text()


def test_a_failing_setup_skips_the_scratch_checks_and_cleans_up_what_was_made(tmp_path, live_config):
    def fail(op, bucket, args):
        if op == "create-bucket" and bucket.endswith("-nv"):
            return "TooManyBuckets", "You have attempted to create more buckets than allowed"
    fake = _fake(fail=fail)
    s, _ = _run(tmp_path, live_config, fake)
    st = _statuses(s)
    assert st["SETUP"] == "FAIL"
    assert all(st[c] == "SKIP" for c in ("S3", "S4", "S5", "S6", "S7", "S8", "S10"))
    assert st["S9"] == "PASS" and s.exit_code == 1
    assert set(fake.deleted) == {s.base, s.ded} and not [b for b in fake.buckets if "-s3smoke-" in b]


def test_the_safety_guard_aborts_the_run_even_when_the_refusal_is_swallowed(tmp_path, live_config, monkeypatch):
    def rogue(self, o):
        try:                          # lifecycle.check-style `except Exception` must NOT swallow it
            lifecycle.write_rules(self.live_base, [], self.admin, self.region, run=self.R)
        except Exception:             # noqa: BLE001
            pass
        o.summary = "the refusal was swallowed"
    monkeypatch.setattr(smoke.Smoke, "s4_shapes", rogue)
    fake = _fake()
    s, printed = _run(tmp_path, live_config, fake)
    st = _statuses(s)
    assert st["S4"] == "FAIL" and all(st[c] == "SKIP" for c in ALL_CHECKS[ALL_CHECKS.index("S5"):])
    assert s.aborted and "SAFETY GUARD" in s.aborted and s.exit_code == 1
    assert fake.live_mutations == [] and fake.live_unchanged()
    assert not [c for c in fake.calls if c["bucket"] == LIVE and c["op"].startswith("put-")]
    _scratch_gone(s, fake)
    report = s.report_path.read_text()
    assert "SAFETY GUARD REFUSED" in report and f"put-bucket-lifecycle-configuration on {LIVE}" in report


def test_the_guard_also_catches_a_refusal_swallowed_by_a_bare_except(tmp_path, live_config, monkeypatch):
    def rogue(self, o):
        try:
            self.R(["s3api", "put-object", "--bucket", LIVE_PHOTOS, "--key", "x", "--body", "/dev/null"],
                   region=self.region, key=self.key, secret=self.secret)
        except BaseException:         # noqa: BLE001 -- the worst case: even BaseException swallowed
            pass
    monkeypatch.setattr(smoke.Smoke, "s5_tier", rogue)
    fake = _fake()
    s, _ = _run(tmp_path, live_config, fake)
    assert _statuses(s)["S5"] == "FAIL" and s.aborted and s.exit_code == 1
    assert fake.live_mutations == [] and fake.live_unchanged()
    _scratch_gone(s, fake)


# --- the guard itself --------------------------------------------------------------------------------

def _guard():
    base, ded, nv = smoke.scratch_names(LIVE, "a1b2c3")
    return smoke.SafetyGuard([base, ded, nv], [LIVE, LIVE_PHOTOS]), base


def test_guard_allows_only_reads_on_live_buckets():
    g, base = _guard()
    for op in ("get-bucket-lifecycle-configuration", "get-bucket-versioning", "get-bucket-location"):
        assert g.verdict(["s3api", op, "--bucket", LIVE]) is None
    for args in (["s3api", "put-bucket-lifecycle-configuration", "--bucket", LIVE, "--lifecycle-configuration", "{}"],
                 ["s3api", "put-bucket-versioning", "--bucket", LIVE_PHOTOS, "--versioning-configuration", "Status=Suspended"],
                 ["s3api", "delete-bucket-lifecycle", "--bucket", LIVE],
                 ["s3api", "delete-bucket", "--bucket", LIVE],
                 ["s3api", "put-object", "--bucket", LIVE, "--key", "k", "--body", "/dev/null"],
                 ["s3api", "delete-object", "--bucket", LIVE, "--key", "k"],
                 ["s3api", "delete-objects", "--bucket", LIVE, "--delete", "{}"],
                 ["s3api", "restore-object", "--bucket", LIVE, "--key", "k"],
                 ["s3api", "create-bucket", "--bucket", "someone-elses-bucket"],
                 ["s3api", "list-object-versions", "--no-paginate", "--cli-input-json", json.dumps({"Bucket": LIVE})],
                 ["s3api", "put-bucket-lifecycle-configuration"],                    # no bucket at all
                 ["iam", "create-user", "--user-name", "x"],
                 ["sts", "assume-role", "--role-arn", "arn"]):
        assert g.verdict(args), args
        with pytest.raises(smoke.SafetyGuardViolation):
            g.wrap(lambda a, **kw: pytest.fail(f"reached the runner: {a}"))(args, region="r", key="k", secret="s")


def test_guard_allows_anything_on_scratch_buckets_and_the_local_calls():
    g, base = _guard()
    assert g.verdict(["--version"]) is None
    assert g.verdict(["s3api", "put-bucket-lifecycle-configuration", "help"]) is None
    assert g.verdict(["s3api", "put-bucket-lifecycle-configuration", "--generate-cli-skeleton", "input"]) is None
    for b in (base, base + "-ded", base + "-nv"):
        assert g.verdict(["s3api", "put-bucket-lifecycle-configuration", "--bucket", b, "--x", "y"]) is None
        assert g.verdict(["s3api", "delete-bucket", "--bucket", b]) is None
        assert g.verdict(["s3api", "list-object-versions", "--cli-input-json", json.dumps({"Bucket": b})]) is None
    assert g.verdict(["s3api", "put-bucket-lifecycle-configuration", "help", "--bucket", LIVE])   # not a help call
    assert g.verdict(["s3api", "put-bucket-lifecycle-configuration", "--generate-cli-skeleton", "input",
                      "--bucket", LIVE])                                                          # not local either


def test_guard_refuses_to_treat_live_or_odd_names_as_scratch():
    with pytest.raises(ValueError):
        smoke.SafetyGuard([LIVE], [LIVE])
    with pytest.raises(ValueError):
        smoke.SafetyGuard(["unraid-backup-123456789012-photos"], [LIVE])
    base, ded, nv = smoke.scratch_names(LIVE, "a1b2c3")
    with pytest.raises(ValueError):
        smoke.SafetyGuard([base], [base])


def test_scratch_names_stay_within_63_characters():
    for live in (LIVE, "a" * 63, "my.dotted.bucket.name", "x" * 45 + "-" + "y" * 10):
        names = smoke.scratch_names(live, "abcdef")
        assert all(len(n) <= 63 and smoke.app_buckets.valid_bucket_name(n) for n in names), names
        assert names[1] == names[0] + "-ded" and names[2] == names[0] + "-nv"
        assert "-s3smoke-abcdef" in names[0]


# --- S9 with the real runner, S12 opt-in, preflight -------------------------------------------------

def test_the_cli_support_probe_runs_the_real_runner_with_no_credentials_in_its_env(monkeypatch):
    """S9's environment inspection, through lifecycle's REAL aws runner -- with a fake subprocess
    module underneath, so nothing is executed."""
    seen = []

    class FakeSubprocess:
        @staticmethod
        def run(cmd, **kw):
            seen.append((cmd, dict(kw["env"])))
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Bucket": "",
                                                                    "TransitionDefaultMinimumObjectSize": ""}),
                                   stderr="")
    monkeypatch.setattr(lifecycle, "subprocess", FakeSubprocess)
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AWS_PROFILE", "someone")
    o = smoke.Outcome("S9", "probe")
    rec = smoke.Recorder(lifecycle._run_aws, smoke.Scrubber(KEY, SECRET, TOKEN), KEY)
    fake_smoke = SimpleNamespace(R=rec, rec=rec, region="eu-west-2", aws_version="aws-cli/2.x")
    smoke.Smoke.s9_probe(fake_smoke, o)
    assert o.fails == [] and "IS supported" in o.summary and "no credentials in its environment" in o.summary
    assert len(seen) == 1 and seen[0][0][:5] == ["aws", "s3api", "put-bucket-lifecycle-configuration",
                                                 "--generate-cli-skeleton", "input"]
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"} & set(seen[0][1])
    assert lifecycle.subprocess is FakeSubprocess            # put back


def test_opentofu_check_is_opt_in(tmp_path, live_config):
    tofu = FakeTofu()
    s, _ = _run(tmp_path, live_config, _fake(), env=dict(CREDS, SMOKE_TOFU="1"), tofu=tofu)
    assert _statuses(s)["S12"] == "PASS" and s.exit_code == 0
    assert [a[0] for a, _ in tofu.calls] == ["version", "init", "validate"]
    assert "-backend=false" in tofu.calls[1][0]


def test_opentofu_asked_for_but_missing_fails_s12_only(tmp_path, live_config):
    s, _ = _run(tmp_path, live_config, _fake(), env=dict(CREDS, SMOKE_TOFU="1"), tofu=FakeTofu(missing=True))
    st = _statuses(s)
    assert st["S12"] == "FAIL" and st["S1"] == "PASS" and "tofu not found" in s.results[0].summary


def test_missing_keys_stop_before_any_aws_call(tmp_path, live_config):
    fake = _fake()
    rc = smoke.main([], run=fake, env={"AWS_ACCESS_KEY_ID": KEY}, out_dir=tmp_path / "out",
                    live_config=live_config, app_root=REPO)
    assert rc == 2 and fake.calls == []


def test_missing_live_config_stops_before_any_aws_call(tmp_path):
    fake = _fake()
    rc = smoke.main([], run=fake, env=dict(CREDS), out_dir=tmp_path / "out", live_config=tmp_path / "nope",
                    app_root=REPO)
    assert rc == 2 and fake.calls == []


# --- run.sh, with a stub `docker` (nothing is executed for real) -------------------------------------

_DOCKER_STUB = r"""#!/usr/bin/env bash
log="$STUB_LOG"
case "$1 $2" in
  "image inspect") [ -z "${STUB_NO_IMAGE:-}" ]; exit $? ;;
  "container inspect")
    [ -z "${STUB_NO_CONTAINER:-}" ] || exit 1
    [ "$3" = "--format" ] && printf '%s\n' "$STUB_LIVE_CONFIG"
    exit 0 ;;
esac
if [ "$1" = "run" ]; then
  printf '%s\n' "$@" > "$log/argv"
  env | grep -E '^(AWS_|SMOKE_)' | sort > "$log/env"
  for a in "$@"; do
    case "$a" in *:/out) touch "${a%:/out}/s3rules-smoke-20260924T000000Z.txt" ;; esac
  done
  exit "${STUB_RC:-0}"
fi
echo "unexpected docker call: $*" >&2
exit 99
"""


@pytest.fixture
def run_sh(tmp_path, live_config):
    import os
    import subprocess
    bindir, log, out = tmp_path / "bin", tmp_path / "log", tmp_path / "smoke-out"
    bindir.mkdir()
    log.mkdir()
    (bindir / "docker").write_text(_DOCKER_STUB)
    (bindir / "docker").chmod(0o755)

    def go(stdin, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "SMOKE_"))}
        env.update(PATH=f"{bindir}:{env['PATH']}", STUB_LOG=str(log), STUB_LIVE_CONFIG=str(live_config),
                   SMOKE_OUT_DIR=str(out), **extra)
        cp = subprocess.run(["bash", str(HERE / "run.sh")], input=stdin, env=env, capture_output=True,
                            text=True, timeout=60)
        argv = (log / "argv").read_text().splitlines() if (log / "argv").exists() else None
        penv = dict(l.split("=", 1) for l in (log / "env").read_text().splitlines()) if (log / "env").exists() else None
        return cp, argv, penv
    return go


def test_run_sh_passes_the_keys_only_through_the_environment(run_sh, live_config, tmp_path):
    cp, argv, penv = run_sh(f"{KEY}\n{SECRET}\n\n", AWS_SESSION_TOKEN="a-stale-token-from-the-shell")
    assert cp.returncode == 0, cp.stderr
    assert argv[:4] == ["run", "--rm", "--entrypoint", "python3"]
    assert argv[-2:] == ["backup-engine:s3rules-smoke", "/smoke/s3rules_smoke.py"]
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert name in argv                                   # `-e NAME`: the value comes from the env
    assert f"{HERE}:/smoke:ro" in argv and f"{live_config}:/liveconfig:ro" in argv
    assert f"{tmp_path / 'smoke-out'}:/out" in argv
    _no_secrets("\n".join(argv), cp.stdout, cp.stderr)
    assert penv["AWS_ACCESS_KEY_ID"] == KEY and penv["AWS_SECRET_ACCESS_KEY"] == SECRET
    assert "AWS_SESSION_TOKEN" not in penv                   # Enter = no token, not the shell's stale one
    assert f"Report: {tmp_path / 'smoke-out'}/s3rules-smoke-20260924T000000Z.txt" in cp.stdout


def test_run_sh_passes_a_session_token_and_the_exit_code(run_sh):
    cp, argv, penv = run_sh(f"{KEY}\n{SECRET}\n{TOKEN}\n", STUB_RC="1", SMOKE_TOFU="1")
    assert cp.returncode == 1
    assert penv["AWS_SESSION_TOKEN"] == TOKEN and penv["SMOKE_TOFU"] == "1"
    _no_secrets("\n".join(argv), cp.stdout, cp.stderr)


@pytest.mark.parametrize("stub, words", [("STUB_NO_IMAGE", "image backup-engine:s3rules-smoke not found"),
                                         ("STUB_NO_CONTAINER", "no container named 'backup-engine'")])
def test_run_sh_stops_clearly_before_asking_for_keys(run_sh, stub, words):
    cp, argv, _ = run_sh(f"{KEY}\n{SECRET}\n\n", **{stub: "1"})
    assert cp.returncode == 1 and words in cp.stderr and argv is None


def test_run_sh_needs_both_keys(run_sh):
    cp, argv, _ = run_sh("\n\n\n")
    assert cp.returncode == 1 and "both needed" in cp.stderr and argv is None


# --- the REAL runner path: lifecycle._run_aws -> subprocess -> an `aws` stub backed by FakeAWS ----------

_AWS_STUB = r'''#!{python}
# `aws` for one test: FakeAWS with its state pickled between calls. Records which credential
# variables each call's environment carried. PATH holds nothing but this stub.
import fcntl, json, os, pickle, sys
sys.path.insert(0, {here!r})
from fake_aws import FakeAWS
spec = json.load(open(os.environ["FAKE_AWS_SPEC"]))
state = os.environ["FAKE_AWS_STATE"]
KEEP = ("buckets", "live", "initial_live", "calls", "cred_errors", "live_mutations", "unknown", "created", "deleted")
with open(state + ".lock", "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    fake = FakeAWS(region=spec["region"], creds=spec["creds"], live=spec["live"])
    envlog = []
    if os.path.exists(state):
        with open(state, "rb") as f:
            saved = pickle.load(f)
        for k in KEEP:
            setattr(fake, k, saved[k])
        envlog = saved["envlog"]
    cp = fake(sys.argv[1:], region=os.environ.get("AWS_DEFAULT_REGION", ""),
              key=os.environ.get("AWS_ACCESS_KEY_ID", ""), secret=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
              session_token=os.environ.get("AWS_SESSION_TOKEN"))
    envlog.append((sys.argv[1:], sorted(k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                                                    "AWS_SESSION_TOKEN", "AWS_PROFILE") if k in os.environ)))
    with open(state, "wb") as f:
        pickle.dump(dict({{k: getattr(fake, k) for k in KEEP}}, envlog=envlog), f)
sys.stdout.write(cp.stdout)
sys.stderr.write(cp.stderr)
sys.exit(cp.returncode)
'''


def test_the_whole_run_through_lifecycles_real_aws_runner(tmp_path, live_config, monkeypatch):
    """execute() with its DEFAULT runner (lifecycle._run_aws, a real subprocess per call) against an
    `aws` stub: proves the argv/env plumbing the box will use -- JSON args survive the command line,
    every call carries the admin keys, and the CLI-support probe's process has no credential variable at
    all. PATH is ONLY the stub, and AWS_ENDPOINT_URL points at a closed local port, so no real aws
    CLI could reach AWS even if one were somehow found."""
    import pickle
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    (bindir / "aws").write_text(_AWS_STUB.format(python=sys.executable, here=str(HERE)))
    (bindir / "aws").chmod(0o755)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"region": "eu-west-2", "creds": CREDS, "live": {
        LIVE: {"versioning": "Enabled", "lifecycle": LIVE_LEGACY, "region": "eu-west-2"},
        LIVE_PHOTOS: {"versioning": "Enabled", "lifecycle": None, "region": "eu-west-2"}}}))
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
              "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FAKE_AWS_SPEC", str(spec))
    monkeypatch.setenv("FAKE_AWS_STATE", str(tmp_path / "state.pickle"))
    out = io.StringIO()
    s = smoke.execute(tofu_run=FakeTofu(), env=dict(CREDS), out_dir=tmp_path / "out", live_config=live_config,
                      app_root=REPO, sleep=lambda x: None, out=out)
    assert s.exit_code == 0, {o.cid: o.fails for o in s.results if o.fails}
    state = pickle.loads((tmp_path / "state.pickle").read_bytes())
    assert state["cred_errors"] == [] and state["live_mutations"] == [] and state["unknown"] == []
    assert not [b for b in state["buckets"] if "-s3smoke-" in b] and set(state["deleted"]) == set(s.created)
    anon = [(a, env) for a, env in state["envlog"] if a == ["--version"] or "--generate-cli-skeleton" in a]
    assert anon and all(env == [] for _, env in anon)          # no credential variable AT ALL
    assert all(env for a, env in state["envlog"] if (a, env) not in anon)
    s9 = next(o for o in s.results if o.cid == "S9")
    assert "no credentials in its environment" in s9.summary
    _no_secrets(s.report_path.read_text(), out.getvalue())
