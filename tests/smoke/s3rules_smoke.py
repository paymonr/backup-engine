#!/usr/bin/env python3
# tests/smoke/s3rules_smoke.py — ONE-SHOT real-AWS smoke test for the S3 rules feature (spec
# docs/superpowers/specs/2026-09-23-s3-rules-design.md). Dev tooling, never shipped: tests/ isn't
# in the image (.dockerignore), so tests/smoke/run.sh bind-mounts this directory as /smoke into a
# freshly built image of the branch and runs `python3 /smoke/s3rules_smoke.py` with
#   /out          writable -- the report (s3rules-smoke-<ts>.txt) and the before-snapshot
#   /liveconfig   the live container's /config, READ-ONLY -- base bucket, region, dedicated buckets
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (/ AWS_SESSION_TOKEN) -- the owner's transient admin keys
#
# What it touches in AWS:
#   * three scratch buckets it creates and ALWAYS deletes again (try/finally, Ctrl-C and SIGTERM
#     included): <live base>-s3smoke-<6 hex> (the scratch "base"), ...-ded (a dedicated bucket)
#     and ...-nv (never versioned);
#   * the live buckets READ-ONLY: get-bucket-lifecycle-configuration / get-bucket-versioning /
#     get-bucket-location for the before-snapshot (S2) -- nothing else. Enforced in code, not by
#     convention: every aws call goes through SafetyGuard, which refuses anything else and aborts
#     the run (straight to cleanup).
#
# Everything else is the app's own code, against a throwaway install (temp CONFIG_DIR / CACHE_DIR /
# SOURCE_ROOT): lifecycle.check / sync / preview / apply_confirmed / save_edit / acknowledge /
# read_lifecycle / write_rules / read_versioning / write_versioning / _min_size_supported,
# jobs_io.upsert, sysop.storage_summary_op + storage_summary.scan / impact, and lifecycle's own
# aws runner (lifecycle._run_aws: provision's credential env plus bounded retries). The two
# credential seams return the admin keys instead of what the scratch install doesn't have:
# lifecycle.role_creds (the bucket-admin role) and sysop._runtime_key (the runtime key).
#
# Checks (S1..S12, in order; each prints PASS / FAIL / SKIP + one line, and a failure never stops
# the next check):
#   S1  environment: aws --version, tofu version, the permissions level this build needs
#   S2  before-snapshot of every live bucket (read-only) -> /out/before-level4-<ts>.json
#   --  SETUP: the scratch buckets and the scratch install
#   S3  legacy round-trip: the exact rules earlier versions wrote (hard-coded from commit 66dadf1)
#       are replaced by the app's own; a second check writes nothing
#   S4  every rule shape the app writes (newest N, newest N + days, 36500 days, newest 100)
#   S5  a cheaper tier through preview + typed confirmation, then moved later (keeps more)
#   S6  versioning: never-versioned bucket; suspend through typed confirmation; back on via sync
#   S7  tamper on real S3: an outside edit is restored + alarmed; a console rule is kept + alarmed
#   S8  killed between the put and the record: the write journal is adopted, nothing re-written
#   S9  the local CLI-support probe for --transition-default-minimum-object-size
#       (`--generate-cli-skeleton input`, no credentials)
#   S10 storage summary on real object versions, paged 2 at a time; impact of shorter rules
#   S11 (skipped) the Verify version-delete probe -- exercised by the permissions Verify
#   S12 (opt-in, SMOKE_TOFU=1) tofu init + validate of the shipped OpenTofu module
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_IMAGE_APP_ROOT = "/app"
if os.path.isdir(os.path.join(_IMAGE_APP_ROOT, "app")) and _IMAGE_APP_ROOT not in sys.path:
    sys.path.insert(0, _IMAGE_APP_ROOT)

from app.engine import buckets as app_buckets  # noqa: E402
from app.engine import lifecycle, storage_summary, sysop  # noqa: E402
from app.gui import jobs_io, permissions  # noqa: E402

# --- the exact lifecycle shapes earlier versions wrote (commit 66dadf1) -------------------------
# opentofu/main.tf @ 66dadf1, resource aws_s3_bucket_lifecycle_configuration.backup: one rule per
# prefix in ["appdata/", "media/"] -- id "backstop-<prefix without />", status Enabled,
# filter { prefix }, noncurrent_version_expiration { noncurrent_days =
# var.noncurrent_version_expiration_days } and abort_incomplete_multipart_upload
# { days_after_initiation = var.abort_incomplete_multipart_days }; opentofu/variables.tf @ 66dadf1
# defaults those to 30 and 7 days.
LEGACY_BASE_RULES = [
    {"ID": "backstop-appdata", "Status": "Enabled", "Filter": {"Prefix": "appdata/"},
     "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
     "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
    {"ID": "backstop-media", "Status": "Enabled", "Filter": {"Prefix": "media/"},
     "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
     "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
]
# app/engine/buckets.py @ 66dadf1, _LIFECYCLE -- what ensure_bucket put on every dedicated bucket
# (note the empty `Filter: {}` form: the whole bucket).
LEGACY_DEDICATED_RULES = [
    {"ID": "backup-engine", "Status": "Enabled", "Filter": {},
     "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
     "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
]

# --- scratch jobs ------------------------------------------------------------------------------
J_PLAIN180, J_FILES, J_SNAP, J_DED = "smk-plain180", "smk-files", "smk-snap", "smk-ded"
J_N10, J_N10D30, J_D36500, J_N100 = "smk-n10", "smk-n10d30", "smk-d36500", "smk-n100"
F_PLAIN180, F_FILES = f"media/{J_PLAIN180}/", f"media/{J_FILES}/"

# --- safety ------------------------------------------------------------------------------------
CRED_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
READ_ONLY_LIVE_OPS = frozenset({"get-bucket-lifecycle-configuration", "get-bucket-versioning",
                                "get-bucket-location"})
CONFIG_PUT_OPS = frozenset({"put-bucket-lifecycle-configuration", "put-bucket-versioning"})
SCRATCH_TAG = "s3smoke"
_SCRATCH_RE = re.compile(r"[a-z0-9][a-z0-9-]*-" + SCRATCH_TAG + r"-[0-9a-f]{6}(?:-ded|-nv)?", re.ASCII)

HARNESS_TIMEOUTS = ["--cli-connect-timeout", "10", "--cli-read-timeout", "60"]
SETTLE_POLLS, SETTLE_INTERVAL_S = 20, 3     # read-after-write: wait up to ~1 minute per write
PROBE_LIMIT_S = 30
S10_ATTEMPTS, S10_RETRY_S = 4, 20
CLEANUP_PAGE = 1000
DELETE_BATCH = 200                             # keeps delete-objects' --delete JSON well under ARG_MAX


class PreflightError(Exception):
    """Can't start (no keys, no /out, no readable live config) -- nothing was sent to AWS."""


class SafetyGuardViolation(BaseException):
    """BaseException on purpose: lifecycle.check() turns any Exception into an "error" state (it
    never raises), so a refused call must be something no `except Exception` can swallow -- it
    aborts the whole run, straight to cleanup."""


class CheckFailed(Exception):
    pass


class CheckSkipped(Exception):
    pass


class AwsError(Exception):
    pass


def scratch_names(live_base: str, hexpart: str) -> tuple[str, str, str]:
    """(scratch base, dedicated, never-versioned): <live base>-s3smoke-<hex>[-ded|-nv], the live
    base cut short when the longest name would pass S3's 63-character limit."""
    suffix = f"-{SCRATCH_TAG}-{hexpart}"
    room = 63 - len(suffix) - len("-ded")
    stem = re.sub(r"[^a-z0-9-]+", "-", live_base.lower())[:room].strip("-") or "backup"
    base = stem + suffix
    return base, base + "-ded", base + "-nv"


def _op_of(args) -> tuple[str | None, str | None]:
    args = list(args)
    if not args or args[0].startswith("-"):
        return None, (args[0] if args else None)
    return args[0], (args[1] if len(args) > 1 else None)


def _bucket_of(args) -> str | None:
    args = list(args)
    if "--bucket" in args:
        i = args.index("--bucket")
        return args[i + 1] if i + 1 < len(args) else None
    if "--cli-input-json" in args:
        i = args.index("--cli-input-json")
        try:
            b = json.loads(args[i + 1]).get("Bucket")
        except (IndexError, ValueError, AttributeError):
            return None
        return b if isinstance(b, str) else None
    return None


def _is_local(args) -> bool:
    """`aws s3api <op> help` or `aws s3api <op> --generate-cli-skeleton ...` (the app's CLI-support
    probe): answered by the CLI itself, never sent to AWS -- as long as it names no bucket."""
    args = list(args)
    return (len(args) >= 3 and (args[2] == "help" or "--generate-cli-skeleton" in args)
            and "--bucket" not in args and "--cli-input-json" not in args)


class SafetyGuard:
    """Every aws call passes verdict() first. Allowed: `aws --version`, a local `aws s3api <op>
    help` / `--generate-cli-skeleton`, ANY s3api call on one of this run's scratch buckets, and on any other bucket only
    get-bucket-lifecycle-configuration / get-bucket-versioning / get-bucket-location. Anything
    else -- a mutating call (create-*, put-*, delete-*, restore-object, ...) or even another read
    on a live bucket, a call with no bucket, another service -- raises SafetyGuardViolation."""

    def __init__(self, scratch, live):
        self.scratch = frozenset(scratch)
        self.live = frozenset(b for b in live if b)
        bad = sorted(b for b in self.scratch if not _SCRATCH_RE.fullmatch(b) or b in self.live)
        if bad or not self.scratch:
            raise ValueError(f"refusing to treat {bad or 'nothing'} as scratch buckets")
        self.refused: list[str] = []

    def verdict(self, args) -> str | None:
        args = list(args)
        service, op = _op_of(args)
        if service is None:
            return None if op == "--version" else f"aws {op}: not a call this test makes"
        if service == "s3api" and _is_local(args):
            return None
        if service != "s3api":
            return f"aws {service} {op}: only s3api calls are allowed"
        bucket = _bucket_of(args)
        if not bucket:
            return f"s3api {op} names no bucket"
        if bucket in self.scratch or op in READ_ONLY_LIVE_OPS:
            return None
        return f"s3api {op} on {bucket}: not one of this run's scratch buckets"

    def wrap(self, run):
        def guarded(args, **kw):
            reason = self.verdict(args)
            if reason:
                self.refused.append(reason)
                raise SafetyGuardViolation(f"SAFETY GUARD refused {reason}")
            return run(args, **kw)
        return guarded


class Scrubber:
    def __init__(self, *values):
        self.values = sorted({v for v in values if v}, key=len, reverse=True)

    def __call__(self, text) -> str:
        s = text if isinstance(text, str) else str(text)
        for v in self.values:
            s = s.replace(v, "[REDACTED]")
        return s


def _trim(s: str, n: int = 1500) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + f" … ({len(s) - n} more chars)"


class Recorder:
    """The outermost wrapper: every call -- refused ones too -- lands in the report."""

    def __init__(self, run, scrub, admin_key):
        self.run, self.scrub, self.admin_key = run, scrub, admin_key
        self.calls: list[dict] = []
        self.label = "START"

    def __call__(self, args, **kw):
        args = list(args)
        service, op = _op_of(args)
        key = kw.get("key") or ""
        entry = {"n": len(self.calls) + 1, "label": self.label, "op": op, "local": _is_local(args),
                 "args": [self.scrub(a) for a in args],
                 "as": "admin" if key and key == self.admin_key else ("no credentials" if not key else "OTHER KEY"),
                 "token": bool(kw.get("session_token"))}
        self.calls.append(entry)
        t0 = time.monotonic()
        try:
            cp = self.run(args, **kw)
        except BaseException as e:
            entry.update(rc=None, s=round(time.monotonic() - t0, 2), out=self.scrub(f"{type(e).__name__}: {e}"))
            raise
        entry.update(rc=cp.returncode, s=round(time.monotonic() - t0, 2))
        if cp.returncode != 0:
            entry["out"] = self.scrub(_trim((cp.stderr or "") + ("\n" + cp.stdout if cp.stdout else "")))
        return cp


def _with_session_token(run, key: str, token: str | None):
    """storage_summary.scan (and so sysop.storage_summary_op) passes only key/secret -- the runtime
    key has no session token. With temporary admin keys, add the token to calls made AS the admin
    key; calls with no key at all (the local CLI-support probe, --version) stay credential-free."""
    if not token:
        return run

    def filled(args, **kw):
        if key and kw.get("key") == key and not kw.get("session_token"):
            kw["session_token"] = token
        return run(args, **kw)
    return filled


class _SubprocessSpy:
    """Stands in for lifecycle's `subprocess` module during S9 only: records the env each aws
    child gets, then runs it for real."""

    def __init__(self, real):
        self._real = real
        self.seen: list[tuple[list, dict]] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, cmd, *a, **kw):
        self.seen.append((list(cmd), dict(kw.get("env") or {})))
        return self._real.run(cmd, *a, **kw)


def _default_tofu_run(args, *, cwd=None, timeout=120):
    env = {k: v for k, v in os.environ.items() if k not in CRED_VARS and k != "AWS_PROFILE"}
    env["TF_IN_AUTOMATION"] = "1"
    return subprocess.run(["tofu", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


class Outcome:
    def __init__(self, cid: str, title: str):
        self.cid, self.title = cid, title
        self.fails: list[str] = []
        self.notes: list[str] = []
        self.summary = ""
        self.status = None
        self.seconds = 0.0
        self.start_call = 0
        self.end_call = 0

    def expect(self, cond, msg: str) -> bool:
        if not cond:
            self.fails.append(msg)
        return bool(cond)

    def require(self, cond, msg: str) -> None:
        if not cond:
            self.fails.append(msg)
            raise CheckFailed(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def skip(self, why: str):
        raise CheckSkipped(why)

    def detail(self) -> str:
        if self.status == "FAIL" and self.fails:
            more = f" (+{len(self.fails) - 1} more, see report)" if len(self.fails) > 1 else ""
            return self.fails[0] + more
        return self.summary


PLAN = [
    ("S1", "Environment", "s1_environment"),
    ("S2", "Before-snapshot of the live buckets (read-only)", "s2_snapshot"),
    ("SETUP", "Scratch buckets + scratch install", "setup_scratch"),
    ("S3", "Legacy round-trip", "s3_legacy"),
    ("S4", "Every rule shape the app writes", "s4_shapes"),
    ("S5", "Cheaper tier via preview + typed confirmation", "s5_tier"),
    ("S6", "Versioning", "s6_versioning"),
    ("S7", "Tamper on real S3", "s7_tamper"),
    ("S8", "Killed between write and record", "s8_kill"),
    ("S9", "aws-cli support probe (small-object threshold flag)", "s9_probe"),
    ("S10", "Storage summary on real S3", "s10_summary"),
    ("S11", "Verify probe dry check", "s11_verify"),
    ("S12", "OpenTofu init + validate", "s12_tofu"),
]
NEEDS_SCRATCH = frozenset({"S3", "S4", "S5", "S6", "S7", "S8", "S10"})
# A check that FAILs after changing storage.json has it put back and the base bucket re-synced, so
# one refusal by S3 (say, a tier) doesn't make every later check fail with it.
ROLLBACK_SETTINGS = frozenset({"S5", "S6"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rule(rules, rid) -> dict | None:
    return next((r for r in rules if isinstance(r, dict) and r.get("ID") == rid), None)


def _canon(v):
    """A TOLERANT form of S3 rules for the harness's own "has S3 caught up yet?" polling -- on
    purpose independent of lifecycle._norm (the thing S3/S4 test): key order, Filter {} vs
    {"Prefix": ""} vs a top-level Prefix, transition list order and numbers-as-strings all
    compare equal. Whether the APP sees S3's copy as equal is what the zero-put checks prove."""
    if isinstance(v, list):
        return sorted((_canon(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(v, dict):
        d = {k: _canon(x) for k, x in v.items() if x is not None}
        if "ID" in d and "Status" in d:                        # a rule
            if "Filter" not in d and "Prefix" in d:
                d["Filter"] = {"Prefix": d.pop("Prefix")}
            if d.get("Filter") == {}:
                d["Filter"] = {"Prefix": ""}
        return d
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return v


def _by_id(rules, app_only: bool = False) -> dict:
    return {r.get("ID"): _canon(r) for r in rules
            if isinstance(r, dict) and (not app_only or lifecycle.is_app_rule(r))}


def _sorted_dicts(v) -> list:
    return sorted((json.dumps(_canon(x), sort_keys=True) for x in (v if isinstance(v, list) else [v] if v else [])))


class Smoke:
    def __init__(self, *, run, tofu_run, env, out_dir, live_config, app_root, sleep, out, creds):
        self.env = env
        self.key = creds["AWS_ACCESS_KEY_ID"]
        self.secret = creds["AWS_SECRET_ACCESS_KEY"]
        self.token = creds.get("AWS_SESSION_TOKEN") or None
        self.admin = {"AWS_ACCESS_KEY_ID": self.key, "AWS_SECRET_ACCESS_KEY": self.secret}
        if self.token:
            self.admin["AWS_SESSION_TOKEN"] = self.token
        self.scrub = Scrubber(self.key, self.secret, self.token or "")
        self.tofu_run = tofu_run
        self.sleep = sleep
        self.out = out
        self.out_dir = Path(out_dir)
        self.live_config = Path(live_config)
        self.app_root = Path(app_root)
        self.started = _now()
        self.ts = self.started.strftime("%Y%m%dT%H%M%SZ")
        self.report_path = self.out_dir / f"s3rules-smoke-{self.ts}.txt"
        self.snapshot_path = self.out_dir / f"before-level4-{self.ts}.json"

        live_env = _read_env(self.live_config / "backup.env")
        self.live_base = live_env.get("S3_BUCKET", "").strip()
        self.region = (live_env.get("AWS_REGION") or "us-east-1").strip()
        self.live_endpoint = live_env.get("S3_ENDPOINT", "").strip()
        self.live_dedicated = _live_dedicated(self.live_config / "jobs.json", self.live_base)
        self.base, self.ded, self.nv = scratch_names(self.live_base, secrets.token_hex(3))
        self.guard = SafetyGuard([self.base, self.ded, self.nv], [self.live_base, *self.live_dedicated.values()])

        self.real_run = run
        self.rec = Recorder(self.guard.wrap(_with_session_token(run, self.key, self.token)), self.scrub, self.key)
        self.R = self.rec
        self.results: list[Outcome] = []
        self.attempted: list[str] = []
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.leftovers: list[str] = []
        self.targets: list[str] = []
        self.cleanup_notes: list[str] = []
        self.settles: list[tuple] = []
        self.interrupted = False
        self.aborted: str | None = None
        self.workdir: Path | None = None
        self.cfg: dict | None = None
        self.setup_ok = False
        self.aws_version = ""
        self.exit_code = 1

    # --- output ------------------------------------------------------------------------------

    def say(self, line: str = "") -> None:
        self.out.write(self.scrub(line) + "\n")
        self.out.flush()

    # --- aws helpers (the harness's own calls, as the admin keys) ------------------------------

    def aws(self, *args, output=True):
        a = ["s3api", *args, *(["--output", "json"] if output else []), *HARNESS_TIMEOUTS]
        return self.R(a, region=self.region, key=self.key, secret=self.secret, session_token=self.token)

    def aws_json(self, *args) -> dict:
        cp = self.aws(*args)
        if cp.returncode != 0:
            raise AwsError(f"s3api {args[0]} failed: {self.scrub(_trim(cp.stderr, 400))}")
        try:
            data = json.loads(cp.stdout or "{}")
        except ValueError:
            raise AwsError(f"s3api {args[0]}: unreadable output")
        return data if isinstance(data, dict) else {}

    def put_lifecycle(self, bucket: str, rules: list[dict]) -> None:
        self.aws_json("put-bucket-lifecycle-configuration", "--bucket", bucket,
                      "--lifecycle-configuration", json.dumps({"Rules": rules}))

    def live_rules(self, bucket: str) -> list[dict]:
        return lifecycle.read_lifecycle(bucket, self.admin, self.region, run=self.R)[0]

    def live_ver(self, bucket: str) -> str:
        return lifecycle.read_versioning(bucket, self.admin, self.region, run=self.R)

    def raw_versioning(self, bucket: str) -> str | None:
        return self.aws_json("get-bucket-versioning", "--bucket", bucket).get("Status")

    # --- read-after-write -------------------------------------------------------------------

    def settle(self, o: Outcome, bucket: str, what: str, rules_ok=None, want_ver: str | None = None) -> bool:
        """S3 bucket configuration reads are eventually consistent: poll until S3 shows `what`
        (rules_ok(live rules) and/or versioning == want_ver) before the next pass reads it, so a
        stale read is never mistaken for the app's behaviour. The wait is recorded in the report."""
        t0 = time.monotonic()
        for poll in range(1, SETTLE_POLLS + 1):
            ok = True
            if rules_ok is not None:
                ok = bool(rules_ok(self.live_rules(bucket)))
            if ok and want_ver is not None:
                ok = self.live_ver(bucket) == want_ver
            if ok:
                waited = time.monotonic() - t0
                self.settles.append((o.cid, bucket, what, poll, round(waited, 1)))
                if poll > 1:
                    o.note(f"S3 showed {what} on {bucket} only after {waited:.0f}s ({poll} reads)")
                return True
            self.sleep(SETTLE_INTERVAL_S)
        self.settles.append((o.cid, bucket, what, SETTLE_POLLS, None))
        # not a FAIL by itself: whatever the check asserts next says what that means
        o.note(f"S3 still didn't show {what} on {bucket} after {SETTLE_POLLS} reads — carried on")
        return False

    def in_step_with_applied(self, bucket: str):
        def ok(rules):
            applied = lifecycle.load_applied(self.cfg["CACHE_DIR"], bucket)
            return (applied is not None and _by_id(rules, app_only=True) == _by_id(applied, app_only=True)
                    and not any(r.get("ID") in lifecycle.LEGACY_IDS for r in rules))
        return ok

    def explain_rewrite(self, o: Outcome, bucket: str, live: list[dict]) -> None:
        """A check that wrote again: say exactly how S3's copy differs from what the app recorded."""
        applied = lifecycle.app_rules_of(lifecycle.load_applied(self.cfg["CACHE_DIR"], bucket) or [])
        got = {r.get("ID"): r for r in live if lifecycle.is_app_rule(r)}
        for rid in sorted(set(applied) | set(got)):
            if rid not in got or rid not in applied or lifecycle._norm(got[rid]) != applied[rid]:
                o.note(f"{bucket} {rid}: S3 returned {json.dumps(got.get(rid), sort_keys=True)} — "
                       f"the app wrote {json.dumps(applied.get(rid), sort_keys=True)}")

    def applied_ver(self, bucket: str) -> str | None:
        v = (lifecycle.load_applied_doc(self.cfg["CACHE_DIR"], bucket) or {}).get("versioning")
        return v if v in lifecycle.VERSIONING_STATES else None

    def status(self, bucket: str) -> dict:
        return lifecycle.load_status(self.cfg["CACHE_DIR"]).get(bucket) or {}

    def config_puts(self, start: int) -> list[dict]:
        return [c for c in self.rec.calls[start:] if c["op"] in CONFIG_PUT_OPS and not c["local"]]

    def check(self, bucket: str, run=None) -> str:
        return lifecycle.check(self.cfg, bucket, run=run or self.R, trigger="manual")

    def recheck_zero_puts(self, o: Outcome, bucket: str, label: str) -> None:
        """Settle, then one more check: it must find S3 in step -- zero puts, state ok, no alarm --
        which proves S3's returned JSON normalizes equal to what the app wrote."""
        self.settle(o, bucket, "the app's last write", self.in_step_with_applied(bucket), self.applied_ver(bucket))
        live_before = self.live_rules(bucket)
        start = len(self.rec.calls)
        state = self.check(bucket)
        puts = self.config_puts(start)
        st = self.status(bucket)
        if puts:
            self.explain_rewrite(o, bucket, live_before)
        o.expect(state == "ok", f"{label}: check on {bucket} returned {state!r} ({st.get('detail', '')})")
        o.expect(not puts, f"{label}: check on {bucket} wrote again ({len(puts)} put(s)) — S3's copy of the "
                           "rules didn't compare equal to what the app wrote")
        o.expect("alarm" not in st, f"{label}: an alarm is open on {bucket}: {st.get('alarm')}")

    # --- scratch install ----------------------------------------------------------------------

    def job(self, name: str, typ: str, retention: dict, **extra) -> dict:
        Path(self.cfg["SOURCE_ROOT"], name).mkdir(parents=True, exist_ok=True)
        return {"name": name, "type": typ, "source": name, "schedule": "0 3 * * *", "enabled": True,
                "storage_class": "STANDARD", "retention": retention, **extra}

    def upsert(self, job: dict) -> None:
        jobs_io.upsert(self.cfg["CONFIG_DIR"], job, source_root=self.cfg["SOURCE_ROOT"])

    def mark_ran(self, name: str, typ: str) -> None:
        """state/<job>.json exactly as backup-job.sh's _write_state leaves it after a run."""
        ts = _iso(_now())
        Path(self.cfg["CACHE_DIR"], "state", f"{name}.json").write_text(json.dumps({
            "last_run": ts, "outcome": "success", "type": typ, "snapshot_id": "", "duration_s": 1,
            "error": "", "exit_code": 0, "run_id": "", "started_at": ts, "finished_at": ts}) + "\n")

    def edit_settings(self, fn) -> dict:
        s = copy.deepcopy(lifecycle.load_settings(self.cfg["CONFIG_DIR"]))
        b = s["buckets"].setdefault(self.base, {})
        b.setdefault("folders", {})
        fn(b)
        return {"kind": "settings", "settings": s}

    # --- the checks -------------------------------------------------------------------------------

    def s1_environment(self, o: Outcome) -> None:
        cp = self.R(["--version"], region=self.region, key="", secret="", session_token=None)
        self.aws_version = (((cp.stdout or "") + (cp.stderr or "")).strip().splitlines() or ["?"])[0]
        o.require(cp.returncode == 0, f"aws --version failed (rc={cp.returncode})")
        try:
            tp = self.tofu_run(["version"], cwd=None, timeout=60)
            tofu = ((tp.stdout or "").strip().splitlines() or ["?"])[0] if tp.returncode == 0 else \
                f"tofu version failed (rc={tp.returncode})"
        except FileNotFoundError:
            tofu = "tofu not found"
        except Exception as e:  # noqa: BLE001 -- informational only
            tofu = f"tofu version: {type(e).__name__}"
        manifest = self.app_root / "provisioning" / "permissions.json"
        level = permissions.required_level(manifest)
        need = permissions.feature_level("s3-rules", manifest)
        o.note(f"lifecycle.py sha256 {_sha(Path(lifecycle.__file__))}; permissions manifest {manifest}")
        o.expect(need is not None and level >= need,
                 f"this build's permissions level {level} is below S3 rules' level {need}")
        o.summary = f"{self.aws_version}; {tofu}; permissions level {level} (S3 rules need {need})"

    def s2_snapshot(self, o: Outcome) -> None:
        targets = [(self.live_base, "base")] + [(b, f"dedicated ({j})") for j, b in sorted(self.live_dedicated.items())]
        snap = {"taken_at": _iso(_now()), "region": self.region,
                "note": "read-only snapshot of the live buckets before the level-4 update (s3rules smoke test)",
                "buckets": {}}
        errors = 0
        for bucket, role in targets:
            entry = {"role": role}
            for op, field in (("get-bucket-lifecycle-configuration", "lifecycle"),
                              ("get-bucket-versioning", "versioning"), ("get-bucket-location", "location")):
                cp = self.aws(op, "--bucket", bucket)
                if cp.returncode == 0:
                    try:
                        entry[field] = json.loads(cp.stdout or "{}")
                    except ValueError:
                        entry[f"{field}_error"] = "unreadable output"
                        errors += 1
                elif op == "get-bucket-lifecycle-configuration" and "NoSuchLifecycleConfiguration" in (cp.stderr or ""):
                    entry[field] = {"Rules": []}
                    entry[f"{field}_note"] = "NoSuchLifecycleConfiguration (no rules)"
                else:
                    entry[f"{field}_error"] = self.scrub(_trim(cp.stderr, 500))
                    errors += 1
            loc = (entry.get("location") or {}).get("LocationConstraint") or "us-east-1"
            if "location" in entry and loc != self.region:
                o.note(f"{bucket} is in {loc}, backup.env says AWS_REGION={self.region}")
            snap["buckets"][bucket] = entry
            o.note(f"{bucket} ({role}): {len((entry.get('lifecycle') or {}).get('Rules') or [])} rule(s), "
                   f"versioning {(entry.get('versioning') or {}).get('Status') or 'never/unknown'}")
        self.snapshot_path.write_text(self.scrub(json.dumps(snap, indent=2, sort_keys=True)) + "\n")
        o.summary = (f"{len(targets)} live bucket(s) saved to {self.snapshot_path}"
                     + (f" — {errors} read error(s) recorded in it" if errors else ""))

    def setup_scratch(self, o: Outcome) -> None:
        o.require(not self.live_endpoint, f"the live install uses a custom S3 endpoint ({self.live_endpoint}) — "
                                          "this smoke test is for AWS S3")
        for bucket in (self.base, self.ded, self.nv):
            o.require(app_buckets.valid_bucket_name(bucket) and len(bucket) <= 63, f"bad scratch bucket name {bucket}")
        loc = [] if self.region == "us-east-1" else ["--create-bucket-configuration", f"LocationConstraint={self.region}"]
        for bucket, versioned in ((self.base, True), (self.ded, True), (self.nv, False)):
            self.attempted.append(bucket)
            cp = self.aws("create-bucket", "--bucket", bucket, "--region", self.region, *loc)
            if cp.returncode != 0 and "BucketAlreadyOwnedByYou" not in (cp.stderr or ""):
                o.require(False, f"create-bucket {bucket}: {self.scrub(_trim(cp.stderr, 300))}")
            self.created.append(bucket)
            if versioned:
                self._retry(lambda b=bucket: self.aws_json("put-bucket-versioning", "--bucket", b,
                                                           "--versioning-configuration", "Status=Enabled"))
        for bucket in (self.base, self.ded):
            self.settle(o, bucket, "versioning Enabled", want_ver="on")
        self.settle(o, self.nv, "no versioning", want_ver="never")

        self.workdir = Path(tempfile.mkdtemp(prefix="s3smoke-"))
        conf, cache, src = self.workdir / "config", self.workdir / "cache", self.workdir / "source"
        for d in (conf, cache / "state", src):
            d.mkdir(parents=True)
        level = permissions.required_level(self.app_root / "provisioning" / "permissions.json")
        (conf / "backup.env").write_text(
            f"S3_BUCKET={self.base}\nAWS_REGION={self.region}\nPERMISSIONS_VERSION={level}\n"
            "BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::000000000000:role/s3smoke-not-used-role-creds-are-patched\n"
            "BASE_BUCKET_VERSIONED=true\nAPPRISE_URLS=\n")
        # never read (sysop._runtime_key is patched) -- and if something did, these fail safe
        (conf / "secrets.env").write_text("AWS_ACCESS_KEY_ID=AKIAS3SMOKEDUMMYKEY0\n"
                                          "AWS_SECRET_ACCESS_KEY=s3smoke-dummy-secret-never-valid\n"
                                          "RESTIC_PASSWORD=s3smoke-dummy\n")
        self.cfg = {"CONFIG_DIR": str(conf), "CACHE_DIR": str(cache), "SOURCE_ROOT": str(src),
                    "SCRIPTS_DIR": str(self.app_root / "scripts")}
        o.require(lifecycle.managed(self.cfg["CONFIG_DIR"]), "the scratch install doesn't count as managed")
        self.setup_ok = True
        o.summary = f"created {self.base} (+ -ded, -nv) in {self.region}; scratch install in {self.workdir}"

    def s3_legacy(self, o: Outcome) -> None:
        base, ded, cache = self.base, self.ded, self.cfg["CACHE_DIR"]
        self.put_lifecycle(base, LEGACY_BASE_RULES)
        self.put_lifecycle(ded, LEGACY_DEDICATED_RULES)
        self.settle(o, base, "the legacy backstops", lambda rules: {r.get("ID") for r in rules} == {"backstop-appdata", "backstop-media"})
        self.settle(o, ded, "the legacy dedicated rule", lambda rules: {r.get("ID") for r in rules} == {"backup-engine"})
        self.upsert(self.job(J_PLAIN180, "archive", {"type": "days", "days": 180}))
        self.upsert(self.job(J_FILES, "versioned-files", {"type": "days", "days": 90}))
        self.upsert(self.job(J_SNAP, "versioned", {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}))
        self.upsert(self.job(J_DED, "archive", {"type": "days", "days": 90}, dedicated=True, bucket=ded, bucket_versioned=True))
        for name, typ in ((J_PLAIN180, "archive"), (J_FILES, "versioned-files"), (J_SNAP, "versioned"), (J_DED, "archive")):
            self.mark_ran(name, typ)

        expect = {
            base: {lifecycle.rule_id("appdata/"): 30, lifecycle.rule_id(F_FILES): 30, lifecycle.rule_id(F_PLAIN180): 180},
            ded: {lifecycle.rule_id(""): 90},
        }
        for bucket in (base, ded):
            state = self.check(bucket)
            st = self.status(bucket)
            o.expect(state == "ok", f"first check on {bucket} returned {state!r} ({st.get('detail', '')})")
            o.expect("alarm" not in st, f"first check on {bucket} raised an alarm: {st.get('alarm')}")
            self.settle(o, bucket, "the app's rules", self.in_step_with_applied(bucket))
            live = self.live_rules(bucket)
            ids = {r.get("ID") for r in live}
            o.expect(not ids & lifecycle.LEGACY_IDS, f"{bucket}: legacy rule(s) still there: {sorted(ids & lifecycle.LEGACY_IDS)}")
            want_ids = set(expect[bucket]) | {lifecycle.HOUSEKEEPING_ID}
            o.expect(ids == want_ids, f"{bucket}: rules are {sorted(ids)}, expected {sorted(want_ids)}")
            for rid, days in expect[bucket].items():
                nce = (_rule(live, rid) or {}).get("NoncurrentVersionExpiration")
                o.expect(_canon(nce) == {"NoncurrentDays": days}, f"{bucket} {rid}: {nce} (expected {days} days)")
            hk = _rule(live, lifecycle.HOUSEKEEPING_ID) or {}
            o.expect(_canon(hk.get("AbortIncompleteMultipartUpload")) == {"DaysAfterInitiation": 7}
                     and (hk.get("Expiration") or {}).get("ExpiredObjectDeleteMarker") is True,
                     f"{bucket}: housekeeping rule is {hk}")
            o.note(f"S3 returned for {bucket}: {json.dumps(live, sort_keys=True)}")
        for bucket in (base, ded):
            self.recheck_zero_puts(o, bucket, "second check")
        o.summary = ("legacy backstop-appdata/backstop-media + dedicated backup-engine rule replaced by the app's own; "
                     "second check on both buckets: 0 puts")

    def s4_shapes(self, o: Outcome) -> None:
        shapes = [
            (J_N10, {"type": "count", "count": 10}, {"NoncurrentDays": 1, "NewerNoncurrentVersions": 10}),
            (J_N10D30, {"type": "count", "count": 10, "days": 30}, {"NoncurrentDays": 30, "NewerNoncurrentVersions": 10}),
            (J_D36500, {"type": "days", "days": 36500}, {"NoncurrentDays": 36500}),
            (J_N100, {"type": "count", "count": 100}, {"NoncurrentDays": 1, "NewerNoncurrentVersions": 100}),
        ]
        for name, retention, _ in shapes:
            self.upsert(self.job(name, "archive", retention))
        state = self.check(self.base)
        st = self.status(self.base)
        if state != "ok":
            o.fails.append(f"check with the four new rule shapes returned {state!r} ({st.get('detail', '')})")
            self._s4_which_shapes(o, shapes)
            return
        o.expect("alarm" not in st, f"an alarm is open: {st.get('alarm')}")
        self.settle(o, self.base, "the new rules", self.in_step_with_applied(self.base))
        live = self.live_rules(self.base)
        for name, _, nce in shapes:
            got = (_rule(live, lifecycle.rule_id(f"media/{name}/")) or {}).get("NoncurrentVersionExpiration")
            o.expect(_canon(got) == nce, f"media/{name}/: S3 has {got}, expected {nce}")
        self.recheck_zero_puts(o, self.base, "second check")
        o.summary = "newest 10 / newest 10 + 30 days / 36500 days / newest 100 all accepted by S3; second check: 0 puts"

    def _s4_which_shapes(self, o: Outcome, shapes) -> None:
        """The four together failed: try each alone to name the one(s) S3 refuses, and remove those
        jobs again so the later checks start from rules S3 accepts."""
        conf, cache = self.cfg["CONFIG_DIR"], self.cfg["CACHE_DIR"]
        for name, _, _ in shapes:
            jobs_io.delete(conf, name, cache)
        self.check(self.base)
        refused = []
        for name, retention, nce in shapes:
            self.upsert(self.job(name, "archive", retention))
            if self.check(self.base) != "ok":
                refused.append(f"{name} {json.dumps(nce)}: {self.status(self.base).get('detail', '')}")
                jobs_io.delete(conf, name, cache)
                self.check(self.base)
        o.fails.append("S3 refused " + " | ".join(refused) if refused else
                       "each shape alone was accepted — only all four together failed")
        o.note("the refused shapes' jobs were removed again so the later checks start clean")

    def s5_tier(self, o: Outcome) -> None:
        base, cfg = self.base, self.cfg
        rid_p, rid_f = lifecycle.rule_id(F_PLAIN180), lifecycle.rule_id(F_FILES)

        def add_tiers(b):
            b["folders"][F_PLAIN180] = {"tier": {"class": "DEEP_ARCHIVE", "after_days": 30}}
            b["folders"][F_FILES] = {"undo_days": 30, "tier": {"class": "GLACIER_IR", "after_days": 1}}
        pv = lifecycle.preview(cfg, base, self.edit_settings(add_tiers))
        o.require(pv.token, f"preview gave no token (keeps less: {[c.words for c in pv.keeps_less]})")
        o.expect(pv.needs_typed, "preview didn't ask for the bucket name to be typed")
        o.expect({rid_p, rid_f} <= {c.rule_id for c in pv.own}, f"preview's own changes: {[c.rule_id for c in pv.own]}")
        res = lifecycle.apply_confirmed(cfg, pv.token, base, run=self.R)
        o.expect(res is not None and res.changed, "apply_confirmed wrote nothing")
        self.settle(o, base, "the tiers", self.in_step_with_applied(base))
        live = self.live_rules(base)
        p, f = _rule(live, rid_p) or {}, _rule(live, rid_f) or {}
        o.expect(_sorted_dicts(p.get("NoncurrentVersionTransitions")) ==
                 _sorted_dicts([{"NoncurrentDays": 30, "StorageClass": "DEEP_ARCHIVE"}]),
                 f"{F_PLAIN180}: S3 has transitions {p.get('NoncurrentVersionTransitions')}")
        o.expect(_canon(p.get("NoncurrentVersionExpiration")) == {"NoncurrentDays": 180}, f"{F_PLAIN180}: {p}")
        o.expect(_sorted_dicts(f.get("NoncurrentVersionTransitions")) ==
                 _sorted_dicts([{"NoncurrentDays": 1, "StorageClass": "GLACIER_IR"}]),
                 f"{F_FILES}: S3 has transitions {f.get('NoncurrentVersionTransitions')}")
        o.note(f"S3 returned: {json.dumps([p, f], sort_keys=True)}")
        self.recheck_zero_puts(o, base, "check after the tier (1)")
        self.recheck_zero_puts(o, base, "check after the tier (2)")

        def later(b):
            b["folders"][F_PLAIN180]["tier"]["after_days"] = 179
        lifecycle.save_edit(cfg, self.edit_settings(later))
        res = lifecycle.sync(cfg, base, run=self.R)
        o.expect(res.changed and not res.waiting, f"moving the tier later: changed={res.changed}, "
                                                  f"waiting={[c.words for c in res.waiting]}")
        self.settle(o, base, "the tier at 179 days", self.in_step_with_applied(base))
        p = _rule(self.live_rules(base), rid_p) or {}
        o.expect(_sorted_dicts(p.get("NoncurrentVersionTransitions")) ==
                 _sorted_dicts([{"NoncurrentDays": 179, "StorageClass": "DEEP_ARCHIVE"}]),
                 f"{F_PLAIN180}: after moving the tier later S3 has {p.get('NoncurrentVersionTransitions')}")
        self.recheck_zero_puts(o, base, "check after moving the tier later")
        o.summary = ("Deep Archive after 30 d + Glacier IR after 1 d applied after typed confirmation; "
                     "moved to 179 d (expiry − 1) via save_edit + sync; every re-check 0 puts")

    def s6_versioning(self, o: Outcome) -> None:
        base, nv, cfg = self.base, self.nv, self.cfg
        v = self.live_ver(nv)
        o.expect(v == "never", f"{nv}: read_versioning says {v!r}, expected 'never'")
        lifecycle.write_versioning(nv, "suspended", self.admin, self.region, run=self.R)
        self.settle(o, nv, "versioning Suspended", want_ver="suspended")
        v = self.live_ver(nv)
        o.expect(v == "suspended", f"{nv}: after write_versioning('suspended') it reads {v!r}")

        def suspend(b):
            b["versioning"] = "suspended"
        pv = lifecycle.preview(cfg, base, self.edit_settings(suspend))
        o.require(pv.token, "preview of a suspend gave no token")
        o.expect(pv.needs_typed, "a suspend didn't ask for the bucket name to be typed")
        o.expect(any(c.rule_id == "versioning" for c in pv.own), f"preview's own changes: {[c.rule_id for c in pv.own]}")
        lifecycle.apply_confirmed(cfg, pv.token, base, run=self.R)
        self.settle(o, base, "versioning Suspended", want_ver="suspended")
        raw = self.raw_versioning(base)
        o.expect(raw == "Suspended", f"get-bucket-versioning on {base} says {raw!r} after the confirmed suspend")
        self.recheck_zero_puts(o, base, "check after the suspend")

        def resume(b):
            b["versioning"] = "on"
        lifecycle.save_edit(cfg, self.edit_settings(resume))
        res = lifecycle.sync(cfg, base, run=self.R)
        o.expect(res.changed and not res.waiting, f"turning versioning back on: changed={res.changed}, "
                                                  f"waiting={[c.words for c in res.waiting]}")
        self.settle(o, base, "versioning Enabled", want_ver="on")
        raw = self.raw_versioning(base)
        o.expect(raw == "Enabled", f"get-bucket-versioning on {base} says {raw!r} after turning it back on")
        self.recheck_zero_puts(o, base, "check after turning versioning back on")
        o.summary = (f"-nv reads never → suspended; {base} suspended through typed confirmation "
                     "(then 0 puts) and back on via sync")

    def s7_tamper(self, o: Outcome) -> None:
        base, cache = self.base, self.cfg["CACHE_DIR"]
        rid = lifecycle.rule_id(F_PLAIN180)
        live = self.live_rules(base)
        tampered = copy.deepcopy(live)
        r = _rule(tampered, rid)
        o.require(r is not None, f"{rid} isn't on {base}")
        r["NoncurrentVersionExpiration"] = {"NoncurrentDays": 1}
        r.pop("NoncurrentVersionTransitions", None)     # S3 refuses a move at/after the expiry
        self.put_lifecycle(base, tampered)
        self.settle(o, base, "the outside edit", lambda rules: _by_id(rules) == _by_id(tampered))
        state = self.check(base)
        st = self.status(base)
        o.expect(state == "restored", f"check after the outside edit returned {state!r} ({st.get('detail', '')})")
        o.expect((st.get("alarm") or {}).get("kind") == "restored", f"alarm after the outside edit: {st.get('alarm')}")
        self.settle(o, base, "the restored rule", self.in_step_with_applied(base))
        nce = (_rule(self.live_rules(base), rid) or {}).get("NoncurrentVersionExpiration")
        o.expect(_canon(nce) == {"NoncurrentDays": 180}, f"{rid} on S3 after the check: {nce}")
        lifecycle.acknowledge(cache, base)
        o.expect("alarm" not in self.status(base), "acknowledge didn't clear the alarm")

        console = {"ID": "smoke-console", "Status": "Enabled", "Filter": {"Prefix": "logs/"}, "Expiration": {"Days": 1}}
        live = self.live_rules(base)
        with_console = [*copy.deepcopy(live), console]
        self.put_lifecycle(base, with_console)
        self.settle(o, base, "the console rule", lambda rules: _by_id(rules) == _by_id(with_console))
        state = self.check(base)
        st = self.status(base)
        alarm = st.get("alarm") or {}
        o.expect(state == "console_rule", f"check after the console rule returned {state!r} ({st.get('detail', '')})")
        o.expect(alarm.get("kind") == "console_rule" and "smoke-console" in (alarm.get("rules") or []),
                 f"alarm after the console rule: {alarm}")
        after = self.live_rules(base)
        got = _rule(after, "smoke-console")
        o.expect(got is not None and _canon(got) == _canon(console),
                 f"the console rule on S3 is now {got}")
        o.expect(_by_id(after, app_only=True) == _by_id(live, app_only=True), "the app's own rules changed")
        lifecycle.acknowledge(cache, base)
        o.expect("alarm" not in self.status(base), "acknowledge didn't clear the console-rule alarm")
        o.summary = "outside edit (180 → 1 day) restored + alarmed; console rule kept unchanged + alarmed; both acknowledged"

    def s8_kill(self, o: Outcome) -> None:
        ded, cfg, cache = self.ded, self.cfg, self.cfg["CACHE_DIR"]
        rid = lifecycle.rule_id("")
        job = next(j for j in jobs_io.load(cfg["CONFIG_DIR"]) if j["name"] == J_DED)
        lifecycle.save_edit(cfg, {"kind": "job", "job": dict(job, retention={"type": "days", "days": 120})})

        def killer(args, **kw):
            cp = self.R(args, **kw)
            if list(args)[:2] == ["s3api", "put-bucket-lifecycle-configuration"] and not _is_local(args) \
                    and cp.returncode == 0:
                raise SystemExit(137)          # as if `timeout` killed the pass right after the put
            return cp
        killed = False
        try:
            self.check(ded, run=killer)
        except SystemExit:
            killed = True
        o.require(killed, "the check never reached a successful put to kill")
        journal = Path(cache, "state", "lifecycle", f"{ded}.inflight.json")
        o.expect(journal.exists(), f"no write journal ({journal.name}) after the kill")
        applied = lifecycle.app_rules_of(lifecycle.load_applied(cache, ded) or [])
        o.expect((applied.get(rid) or {}).get("NoncurrentVersionExpiration") == {"NoncurrentDays": 90},
                 f"the applied record already moved before the kill: {applied.get(rid)}")
        self.settle(o, ded, "the killed pass's put",
                    lambda rules: _canon((_rule(rules, rid) or {}).get("NoncurrentVersionExpiration")) == {"NoncurrentDays": 120})
        start = len(self.rec.calls)
        state = self.check(ded)
        st = self.status(ded)
        puts = self.config_puts(start)
        o.expect(state == "ok", f"check after the kill returned {state!r} ({st.get('detail', '')})")
        o.expect("alarm" not in st, f"check after the kill raised an alarm: {st.get('alarm')}")
        o.expect(not journal.exists(), "the write journal is still there")
        o.expect(not puts, f"check after the kill wrote again ({len(puts)} put(s))")
        applied = lifecycle.app_rules_of(lifecycle.load_applied(cache, ded) or [])
        o.expect((applied.get(rid) or {}).get("NoncurrentVersionExpiration") == {"NoncurrentDays": 120},
                 f"the applied record after the adopting check: {applied.get(rid)}")
        o.summary = "killed right after the put: journal left; next check adopted it — ok, no alarm, 0 puts, journal gone"

    def s9_probe(self, o: Outcome) -> None:
        def probe_run(args, **kw):          # a fresh run object: lifecycle caches the probe per run
            return self.R(args, **kw)
        spy = _SubprocessSpy(lifecycle.subprocess)
        start = len(self.rec.calls)
        t0 = time.monotonic()
        lifecycle.subprocess = spy
        try:
            supported = lifecycle._min_size_supported(probe_run, self.region)
        finally:
            lifecycle.subprocess = spy._real
            lifecycle._MIN_SIZE_PROBED[:] = [(r, s) for r, s in lifecycle._MIN_SIZE_PROBED if r is not probe_run]
        took = time.monotonic() - t0
        calls = self.rec.calls[start:]
        o.expect(len(calls) == 1 and calls[0]["args"] == ["s3api", "put-bucket-lifecycle-configuration",
                                                           "--generate-cli-skeleton", "input"],
                 f"the probe made {[c['args'] for c in calls]}")
        o.expect(all(c["as"] == "no credentials" and not c["token"] for c in calls),
                 "the probe was given credentials")
        o.expect(took < PROBE_LIMIT_S, f"the probe took {took:.1f}s (limit {PROBE_LIMIT_S}s)")
        if spy.seen:
            leaked = sorted({k for _, env in spy.seen for k in (*CRED_VARS, "AWS_PROFILE") if k in env})
            o.expect(not leaked, f"the probe's aws process had {leaked} in its environment")
            env_words = "no credentials in its environment"
        else:
            env_words = "no credentials passed (runner isn't the aws CLI: environment not inspected)"
        rc = calls[0].get("rc") if calls else None
        if rc not in (0, None):
            o.note(f"`--generate-cli-skeleton input` exited rc={rc}: {calls[0].get('out', '')[:300]}")
        o.summary = (f"--transition-default-minimum-object-size {'IS' if supported else 'is NOT'} supported by "
                     f"this aws CLI ({self.aws_version}); skeleton rc={rc}; {took:.1f}s; {env_words}")

    def s10_summary(self, o: Outcome) -> None:
        base, cfg, folder = self.base, self.cfg, F_PLAIN180
        bodies = {}
        body_dir = self.workdir / "bodies"
        body_dir.mkdir(exist_ok=True)
        plan = [("a.txt", 1), ("a.txt", 2), ("a.txt", 3), ("b.txt", 1), ("b.txt", 2), ("c.txt", 1)]
        for n, (name, i) in enumerate(plan):
            p = body_dir / f"{name}.{i}"
            p.write_text(f"{name} v{i} " + "x" * (n + 1))       # every version a different size
            bodies[(name, i)] = p
        for attempt in range(1, S10_ATTEMPTS + 1):
            vids = []
            for name, i in plan:
                vids.append(self.aws_json("put-object", "--bucket", base, "--key", folder + name,
                                          "--body", str(bodies[(name, i)])).get("VersionId"))
            d = self.aws_json("delete-object", "--bucket", base, "--key", folder + "c.txt")
            vids.append(d.get("VersionId"))
            o.expect(d.get("DeleteMarker") is True, f"deleting c.txt didn't leave a delete marker: {d}")
            if all(v and v != "null" for v in vids):
                break
            o.note(f"attempt {attempt}: S3 gave unversioned writes ({vids}) — versioning not in effect yet")
            self._purge(base, folder)
            if attempt == S10_ATTEMPTS:
                o.require(False, "S3 kept writing unversioned objects — versioning never took effect")
            self.sleep(S10_RETRY_S)

        entries = self._list_all(base, folder)
        per_key: dict[str, int] = {}
        markers = 0
        for e in entries:
            if e["marker"]:
                markers += 1
            elif not e["latest"]:
                per_key[e["key"].rsplit("/", 1)[-1]] = per_key.get(e["key"].rsplit("/", 1)[-1], 0) + 1
        o.expect(per_key == {"a.txt": 2, "b.txt": 1, "c.txt": 1} and markers == 1,
                 f"S3 lists old versions {per_key} and {markers} delete marker(s)")

        size = {k: v.stat().st_size for k, v in bodies.items()}
        old_bytes = size[("a.txt", 1)] + size[("a.txt", 2)] + size[("b.txt", 1)] + size[("c.txt", 1)]
        cur_bytes = size[("a.txt", 3)] + size[("b.txt", 2)]
        start = len(self.rec.calls)
        with _page_keys(2):
            sysop.storage_summary_op(cfg, log=lambda m: o.note(f"summary: {m}"), job=J_PLAIN180, run=self.R)
        pages = [c for c in self.rec.calls[start:] if c["op"] == "list-object-versions"]
        o.expect(len(pages) >= 2, f"the scan read {len(pages)} page(s) — paging (2 per page) not exercised")
        s = storage_summary.load(cfg["CACHE_DIR"], base, folder)
        o.require(s is not None, "no storage summary was saved")
        got = {k: s.get(k) for k in ("noncurrent_versions", "noncurrent_bytes", "delete_markers",
                                     "current_objects", "current_bytes")}
        want = {"noncurrent_versions": 4, "noncurrent_bytes": old_bytes, "delete_markers": 1,
                "current_objects": 2, "current_bytes": cur_bytes}
        o.expect(got == want, f"summary {got}, expected {want}")
        ranks = {row[0]: row[1] for row in s.get("noncurrent_by_rank") or []}
        o.expect(ranks == {1: 3, 2: 1}, f"old versions by rank {ranks}, expected {{1: 3, 2: 1}}")

        rid = lifecycle.rule_id(folder)
        current = lifecycle.app_rules_of(lifecycle.load_applied(cfg["CACHE_DIR"], base) or []).get(rid)
        o.require(current is not None, f"no applied rule for {folder}")
        one_day = {"ID": rid, "Status": "Enabled", "Filter": {"Prefix": folder},
                   "NoncurrentVersionExpiration": {"NoncurrentDays": 1}}
        newest1 = {"ID": rid, "Status": "Enabled", "Filter": {"Prefix": folder},
                   "NoncurrentVersionExpiration": {"NoncurrentDays": 1, "NewerNoncurrentVersions": 1}}
        now_imp = storage_summary.impact(s, current, one_day)
        o.note(f"impact of a 1-day rule today (ages 0 — informational): {now_imp}")
        aged = storage_summary.scan(base, folder, region=self.region, key=self.key, secret=self.secret,
                                    run=self.R, now=_now() + timedelta(days=3), page_keys=2)
        imp1, impn = storage_summary.impact(aged, current, one_day), storage_summary.impact(aged, current, newest1)
        o.expect(imp1["versions"] == 4 and imp1["bytes"] == old_bytes,
                 f"3 days on, a 1-day rule would remove {imp1} (expected all 4 old versions, {old_bytes} B)")
        o.expect(impn["versions"] == 1 and impn["bytes"] == size[("a.txt", 1)],
                 f"3 days on, newest-1 would remove {impn} (expected a.txt's oldest only)")
        o.summary = (f"old versions a:2 b:1 c:1, 1 delete marker, 2 current — summary read {len(pages)} pages of 2; "
                     f"impact 3 days on: 1-day rule {imp1['versions']}, newest-1 rule {impn['versions']}")

    def s11_verify(self, o: Outcome) -> None:
        o.skip("the version-delete probe is exercised for real by the permissions Verify after the level-4 update")

    def s12_tofu(self, o: Outcome) -> None:
        if self.env.get("SMOKE_TOFU") != "1":
            o.skip("opt-in (downloads the AWS provider): run with SMOKE_TOFU=1 — "
                   "e.g. `SMOKE_TOFU=1 bash run.sh`")
        work = Path(tempfile.mkdtemp(prefix="s3smoke-tofu-"))
        try:
            ignore = shutil.ignore_patterns(".terraform", "*.tfstate", "*.tfstate.*", "*.tfvars")
            shutil.copytree(self.app_root / "opentofu", work / "opentofu", ignore=ignore)
            shutil.copytree(self.app_root / "provisioning", work / "provisioning")
            for args, limit in ((["init", "-backend=false", "-input=false", "-no-color"], 600),
                                (["validate", "-no-color"], 120)):
                try:
                    cp = self.tofu_run(args, cwd=str(work / "opentofu"), timeout=limit)
                except FileNotFoundError:
                    o.require(False, "tofu isn't installed here")
                o.note(f"tofu {args[0]} rc={cp.returncode}: {_trim((cp.stdout or '') + (cp.stderr or ''), 600)}")
                o.require(cp.returncode == 0, f"tofu {args[0]} failed: {_trim(cp.stderr or cp.stdout, 300)}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        o.summary = "tofu init -backend=false + validate accept the module (the `removed` block, ignore_changes)"

    # --- small helpers ---------------------------------------------------------------------------

    def _retry(self, fn, tries: int = 4, wait: float = 2.0):
        for i in range(tries):
            try:
                return fn()
            except AwsError as e:
                if i == tries - 1 or "NoSuchBucket" not in str(e):
                    raise
                self.sleep(wait)

    def _list_all(self, bucket: str, prefix: str = "") -> list[dict]:
        out, marker = [], None
        for _ in range(100000):
            inp = {"Bucket": bucket, "MaxKeys": CLEANUP_PAGE}
            if prefix:
                inp["Prefix"] = prefix
            if marker:
                inp["KeyMarker"], inp["VersionIdMarker"] = marker
            page = self.aws_json("list-object-versions", "--no-paginate", "--cli-input-json", json.dumps(inp))
            for v in page.get("Versions") or []:
                out.append({"key": v.get("Key"), "vid": v.get("VersionId"), "latest": bool(v.get("IsLatest")), "marker": False})
            for m in page.get("DeleteMarkers") or []:
                out.append({"key": m.get("Key"), "vid": m.get("VersionId"), "latest": bool(m.get("IsLatest")), "marker": True})
            if not page.get("IsTruncated"):
                return out
            nxt = (page.get("NextKeyMarker"), page.get("NextVersionIdMarker") or "null")
            if not nxt[0] or nxt == marker:
                raise AwsError(f"list-object-versions on {bucket} stopped advancing")
            marker = nxt
        raise AwsError(f"list-object-versions on {bucket} never ended")

    def _purge(self, bucket: str, prefix: str = "") -> int:
        entries = self._list_all(bucket, prefix)
        objs = [{"Key": e["key"], "VersionId": e["vid"]} for e in entries]
        for i in range(0, len(objs), DELETE_BATCH):
            resp = self.aws_json("delete-objects", "--bucket", bucket,
                                 "--delete", json.dumps({"Objects": objs[i:i + DELETE_BATCH], "Quiet": True}))
            if resp.get("Errors"):
                raise AwsError(f"delete-objects on {bucket}: {self.scrub(json.dumps(resp['Errors'])[:400])}")
        return len(objs)

    # --- cleanup ---------------------------------------------------------------------------------

    def cleanup(self) -> None:
        self.rec.label = "CLEANUP"
        if not self.attempted:
            return
        self.say("")
        self.say("CLEANUP — deleting the scratch buckets (please wait; Ctrl-C is ignored until this finishes)")
        targets = list(self.created)
        for bucket in self.attempted:
            if bucket in targets:
                continue
            # create-bucket reported an error: clean it only if it exists AND is ours (a timeout after
            # S3 created it); a name someone else owns answers 403 and is left alone.
            try:
                if self.aws("head-bucket", "--bucket", bucket, output=False).returncode == 0:
                    targets.append(bucket)
            except Exception as e:                      # noqa: BLE001
                self.cleanup_notes.append(f"{bucket}: head-bucket: {type(e).__name__}: {self.scrub(str(e))}")
        self.targets = targets
        for bucket in targets:
            try:
                ok = self._delete_bucket(bucket)
            except SafetyGuardViolation as e:          # can't happen for a scratch bucket; never hide it
                self.cleanup_notes.append(f"{bucket}: {e}")
                ok = False
            except Exception as e:                      # noqa: BLE001 -- report and carry on
                self.cleanup_notes.append(f"{bucket}: {type(e).__name__}: {self.scrub(str(e))}")
                ok = False
            if ok:
                self.deleted.append(bucket)
                self.say(f"  deleted {bucket}")
        for bucket in targets:
            if bucket in self.deleted and not self._still_there(bucket):
                continue
            if bucket not in self.leftovers:
                self.leftovers.append(bucket)
        for bucket in self.leftovers:
            self.say(f"!!! LEFTOVER SCRATCH BUCKET: {bucket} — delete it by hand "
                     "(AWS console → S3 → the bucket → Empty, then Delete)")
        for n in self.cleanup_notes:
            self.say(f"  cleanup: {n}")
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def _delete_bucket(self, bucket: str) -> bool:
        for attempt in range(3):
            head = self.aws("head-bucket", "--bucket", bucket, output=False)
            if head.returncode != 0 and _gone(head.stderr):
                return True
            n = self._purge(bucket)
            if n:
                self.cleanup_notes.append(f"{bucket}: deleted {n} object version(s)/delete marker(s)")
            cp = self.aws("delete-bucket-lifecycle", "--bucket", bucket, output=False)
            if cp.returncode != 0 and not _gone(cp.stderr):
                self.cleanup_notes.append(f"{bucket}: delete-bucket-lifecycle: {self.scrub(_trim(cp.stderr, 200))}")
            cp = self.aws("delete-bucket", "--bucket", bucket, "--region", self.region, output=False)
            if cp.returncode == 0 or _gone(cp.stderr):
                return True
            self.cleanup_notes.append(f"{bucket}: delete-bucket attempt {attempt + 1}: {self.scrub(_trim(cp.stderr, 200))}")
            self.sleep(2)
        return False

    def _still_there(self, bucket: str) -> bool:
        for _ in range(5):
            cp = self.aws("head-bucket", "--bucket", bucket, output=False)
            if cp.returncode != 0 and _gone(cp.stderr):
                return False
            self.sleep(2)
        return True

    # --- running + report --------------------------------------------------------------------

    def run_checks(self) -> None:
        stop = None
        for cid, title, method in PLAN:
            o = Outcome(cid, title)
            self.results.append(o)
            if stop:
                o.status, o.summary = "SKIP", f"not run: {stop}"
                self.say(f"{cid:<6}SKIP  {title} — {o.summary}")
                continue
            if cid in NEEDS_SCRATCH and not self.setup_ok:
                o.status, o.summary = "SKIP", "not run: the scratch setup failed"
                self.say(f"{cid:<6}SKIP  {title} — {o.summary}")
                continue
            self.rec.label = cid
            o.start_call = len(self.rec.calls)
            self.say(f"{cid:<6}…     {title}")
            t0 = time.monotonic()
            settings_before = self._settings_bytes() if cid in ROLLBACK_SETTINGS and self.setup_ok else False
            try:
                getattr(self, method)(o)
                o.status = "FAIL" if o.fails else "PASS"
            except CheckSkipped as e:
                o.status, o.summary = ("FAIL" if o.fails else "SKIP"), str(e)
            except CheckFailed:
                o.status = "FAIL"
            except KeyboardInterrupt:
                o.status = "FAIL"
                o.fails.append("interrupted (Ctrl-C / SIGTERM)")
                self.interrupted, stop = True, "interrupted"
            except SafetyGuardViolation as e:
                o.status = "FAIL"
                o.fails.insert(0, str(e))
                self.aborted = stop = str(e)
            except SystemExit as e:
                o.status = "FAIL"
                o.fails.append(f"unexpected SystemExit({e.code})")
            except Exception as e:      # noqa: BLE001 -- a check's crash is its FAIL, never the run's
                o.status = "FAIL"
                o.fails.append(f"{type(e).__name__}: {_exc_words(e)}")
                o.note(self.scrub(traceback.format_exc()))
            if self.guard.refused and not self.aborted:
                # belt and braces: a refusal something swallowed still aborts the run
                o.status = "FAIL"
                self.aborted = stop = f"SAFETY GUARD refused {self.guard.refused[0]}"
                o.fails.insert(0, self.aborted)
            if o.status == "FAIL" and settings_before is not False and not stop:
                try:
                    self._rollback_settings(o, settings_before)
                except KeyboardInterrupt:
                    self.interrupted, stop = True, "interrupted"
                except SafetyGuardViolation as e:
                    self.aborted = stop = str(e)
                    o.fails.insert(0, str(e))
            o.seconds = time.monotonic() - t0
            o.end_call = len(self.rec.calls)
            self.say(f"{cid:<6}{o.status:<5} {title} ({o.seconds:.1f}s) — {o.detail()}")
            self.save_report()

    def _settings_bytes(self) -> bytes | None:
        try:
            return Path(self.cfg["CONFIG_DIR"], lifecycle.SETTINGS_FILE).read_bytes()
        except OSError:
            return None

    def _rollback_settings(self, o: Outcome, before: bytes | None) -> None:
        conf, cache = self.cfg["CONFIG_DIR"], self.cfg["CACHE_DIR"]
        try:
            with lifecycle.settings_lock(conf):
                p = Path(conf, lifecycle.SETTINGS_FILE)
                if before is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(before)
            lifecycle.sync(self.cfg, self.base, run=self.R)
            lifecycle.acknowledge(cache, self.base)
            o.note("storage.json put back as it was before this check and the base bucket re-synced, "
                   "so the later checks start clean")
        except (KeyboardInterrupt, SafetyGuardViolation):
            raise
        except Exception as e:      # noqa: BLE001
            o.note(f"storage.json put back, but re-syncing the base bucket failed: {type(e).__name__}: {_exc_words(e)}")

    def save_report(self, final: bool = False) -> None:
        try:
            self.write_report(final)
        except Exception as e:          # noqa: BLE001 -- the report must never stop the run or its cleanup
            self.say(f"  (couldn't write the report {self.report_path}: {type(e).__name__}: {e})")

    def write_report(self, final: bool = False) -> None:
        L = ["backup-engine · S3 rules · real-AWS smoke test", "",
             f"started   {_iso(self.started)}" + (f"   finished {_iso(_now())}" if final else "   (in progress)"),
             f"app       {self.app_root} (lifecycle.py sha256 {_sha(Path(lifecycle.__file__))}; "
             f"git {self.env.get('SMOKE_GIT_REV') or 'unknown'})",
             f"region    {self.region}",
             f"live      base {self.live_base}; dedicated {sorted(self.live_dedicated.values()) or 'none'} (read-only)",
             f"scratch   {self.base}, {self.ded}, {self.nv}", "", "RESULTS"]
        for o in self.results:
            L.append(f"  {o.cid:<6}{o.status or '…':<5} {o.title} ({o.seconds:.1f}s) — {o.detail()}")
        if final:
            L += ["", "CLEANUP",
                  f"  deleted: {', '.join(self.deleted) or 'nothing (no scratch bucket was created)'}",
                  f"  LEFTOVER BUCKETS: {', '.join(self.leftovers)}" if self.leftovers else "  leftovers: none",
                  *(f"  {n}" for n in self.cleanup_notes)]
            waits = [s for s in self.settles if s[3] > 1 or s[4] is None]
            L += ["", "READ-AFTER-WRITE (S3 bucket configuration is eventually consistent)",
                  f"  {len(self.settles)} settle wait(s); {len(waits)} needed more than one read"]
            L += [f"  {cid}: {b} — {what}: {n} read(s), {s if s is not None else 'NEVER'}s" for cid, b, what, n, s in waits]
            if self.guard.refused:
                L += ["", "SAFETY GUARD REFUSED", *(f"  {r}" for r in self.guard.refused)]
            L += ["", f"OVERALL   {self.overall()}"]
        L += ["", "DETAILS"]
        for o in self.results:
            L += ["", f"[{o.cid}] {o.title} — {o.status or '…'}"]
            L += [f"  FAIL: {f}" for f in o.fails]
            L += [f"  note: {n}" for n in o.notes]
            calls = self.rec.calls[o.start_call:o.end_call]
            if calls:
                L.append("  aws calls:")
                L += [_call_line(c) for c in calls]
        cleanup_calls = [c for c in self.rec.calls if c["label"] == "CLEANUP"]
        if cleanup_calls:
            L += ["", "[CLEANUP] aws calls:", *(_call_line(c) for c in cleanup_calls)]
        if final and self.cfg:
            try:
                status = lifecycle.load_status(self.cfg["CACHE_DIR"])
            except Exception:  # noqa: BLE001
                status = {}
            L += ["", "SCRATCH INSTALL: final S3 rules status (state/_lifecycle.json)", json.dumps(status, indent=2, sort_keys=True)]
        self.report_path.write_text(self.scrub("\n".join(L)) + "\n")

    def overall(self) -> str:
        fails = [o.cid for o in self.results if o.status == "FAIL"]
        if self.interrupted:
            return "INTERRUPTED" + (f" (FAIL: {', '.join(fails)})" if fails else "")
        if self.aborted:
            return f"ABORTED by the safety guard (FAIL: {', '.join(fails)})"
        if fails or self.leftovers:
            return "FAIL" + (f" ({', '.join(fails)})" if fails else "") + (" + LEFTOVER BUCKETS" if self.leftovers else "")
        return "PASS"


# --- module-level helpers ----------------------------------------------------------------------

def _read_env(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = re.sub(r"\s#.*$", "", v).strip().strip('"').strip("'")
    return out


def _live_dedicated(path: Path, base: str) -> dict:
    """{job: bucket} for each dedicated job in the live jobs.json (unreadable -> none)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    out = {}
    for j in jobs if isinstance(jobs, list) else []:
        if isinstance(j, dict) and j.get("dedicated") and isinstance(j.get("bucket"), str) \
                and j["bucket"].strip() and j["bucket"].strip() != base:
            out[str(j.get("name"))] = j["bucket"].strip()
    return out


def _gone(stderr: str) -> bool:
    s = stderr or ""
    return any(w in s for w in ("NoSuchBucket", "(404)", "Not Found"))


def _sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    except OSError:
        return "?"


def _exc_words(e: BaseException) -> str:
    kind, detail = getattr(e, "kind", None), getattr(e, "detail", None) or getattr(e, "message", None)
    return f"{kind}: {detail}" if kind and detail else (str(detail or e) or type(e).__name__)


def _call_line(c: dict) -> str:
    line = (f"    #{c['n']:<4} rc={c.get('rc')!s:<4} {c.get('s', 0):>6.2f}s  as {c['as']}"
            f"{' +token' if c['token'] else ''}  aws {' '.join(c['args'])}")
    if c.get("out"):
        line += "\n" + "\n".join(f"           | {ln}" for ln in c["out"].splitlines())
    return line


@contextmanager
def _page_keys(n: int):
    """storage_summary.scan's page size is bound as a default at definition time -- force both."""
    old_const, old_default = storage_summary.PAGE_KEYS, storage_summary.scan.__kwdefaults__["page_keys"]
    storage_summary.PAGE_KEYS = n
    storage_summary.scan.__kwdefaults__["page_keys"] = n
    try:
        yield
    finally:
        storage_summary.PAGE_KEYS = old_const
        storage_summary.scan.__kwdefaults__["page_keys"] = old_default


@contextmanager
def _patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextmanager
def _signals_raise_interrupt():
    """SIGTERM (docker stop) behaves like Ctrl-C: the checks stop and cleanup runs. As PID 1 in a
    container, a signal with no handler would otherwise be ignored and end in SIGKILL."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def on_term(signum, frame):
        raise KeyboardInterrupt
    old = signal.signal(signal.SIGTERM, on_term)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


@contextmanager
def _signals_ignored():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def execute(*, run=None, tofu_run=None, env=None, out_dir=None, live_config=None, app_root=None,
            sleep=time.sleep, out=None) -> Smoke:
    """The whole smoke run: preflight (no AWS), S1..S12, then cleanup -- always."""
    env_is_process = env is None
    env = os.environ if env is None else env
    out = out or sys.stdout
    out_dir = Path(out_dir or env.get("SMOKE_OUT") or "/out")
    live_config = Path(live_config or env.get("SMOKE_LIVECONFIG") or "/liveconfig")
    app_root = Path(app_root or env.get("SMOKE_APP_ROOT") or _IMAGE_APP_ROOT)
    creds = {k: (env.get(k) or "").strip() for k in CRED_VARS}
    if not creds["AWS_ACCESS_KEY_ID"] or not creds["AWS_SECRET_ACCESS_KEY"]:
        raise PreflightError("AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (the admin keys) must be set")
    if env_is_process:
        for k in CRED_VARS:        # nothing this process spawns inherits them; calls pass them explicitly
            os.environ.pop(k, None)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / f".s3smoke-write-test-{os.getpid()}"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise PreflightError(f"can't write to {out_dir}: {e}")
    if not (live_config / "backup.env").is_file():
        raise PreflightError(f"no backup.env in {live_config} — mount the live /config there (read-only)")
    if not _read_env(live_config / "backup.env").get("S3_BUCKET", "").strip():
        raise PreflightError(f"{live_config}/backup.env has no S3_BUCKET")
    if not (app_root / "provisioning" / "permissions.json").is_file():
        raise PreflightError(f"no provisioning/permissions.json under {app_root}")

    try:
        smoke = Smoke(run=run or lifecycle._run_aws, tofu_run=tofu_run or _default_tofu_run, env=env,
                      out_dir=out_dir, live_config=live_config, app_root=app_root, sleep=sleep, out=out,
                      creds=creds)
    except (ValueError, OSError) as e:
        raise PreflightError(f"can't set up the scratch bucket names: {e}")
    smoke.say("backup-engine S3 rules — real-AWS smoke test")
    smoke.say(f"  live (READ-ONLY): {smoke.live_base} + {len(smoke.live_dedicated)} dedicated bucket(s), {smoke.region}")
    smoke.say(f"  scratch (created and deleted again): {smoke.base}, {smoke.ded}, {smoke.nv}")
    smoke.say("  Ctrl-C stops the checks; the scratch buckets are still deleted.")
    smoke.say("")
    probed_before = list(lifecycle._MIN_SIZE_PROBED)
    with ExitStack() as stack:
        stack.enter_context(_patched(lifecycle, "role_creds", lambda config_dir, region, *, run=None: dict(smoke.admin)))
        stack.enter_context(_patched(sysop, "_runtime_key", lambda config_dir: (smoke.key, smoke.secret)))
        stack.enter_context(_patched(lifecycle, "_send_notification", lambda urls, title, body: False))
        try:
            with _signals_raise_interrupt():
                smoke.run_checks()
        except KeyboardInterrupt:
            smoke.interrupted = True
        except SafetyGuardViolation as e:          # outside any check: still abort, still clean up
            smoke.aborted = smoke.aborted or str(e)
        finally:
            with _signals_ignored():
                try:
                    smoke.cleanup()
                finally:
                    lifecycle._MIN_SIZE_PROBED[:] = probed_before     # every run object this test made
                    smoke.save_report(final=True)
    overall = smoke.overall()
    smoke.exit_code = 130 if smoke.interrupted else (0 if overall == "PASS" else 1)
    smoke.say("")
    smoke.say(f"OVERALL: {overall}")
    smoke.say(f"Report: {smoke.report_path}")
    return smoke


def main(argv=None, **kw) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print("usage: python3 s3rules_smoke.py  (inside the image, via tests/smoke/run.sh -- "
              "see the header of this file for the mounts and what it checks)")
        return 0
    try:
        return execute(**kw).exit_code
    except PreflightError as e:
        print(f"s3rules smoke: {e} — nothing was sent to AWS", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
