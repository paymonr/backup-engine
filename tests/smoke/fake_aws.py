# tests/smoke/fake_aws.py — an in-memory stand-in for every `aws` CLI call the S3 rules smoke test
# (tests/smoke/s3rules_smoke.py) makes, so tests/smoke/test_s3rules_smoke.py can run the WHOLE
# script without AWS. Same call shape as lifecycle._run_aws / provision._run_aws:
#   fake(args, *, region, key, secret, session_token=None) -> (returncode, stdout, stderr)
# and it emulates what S3 itself does wherever the smoke test's verdicts depend on it:
#   * create-bucket: LocationConstraint required outside us-east-1 and refused inside it;
#   * versioning: never-versioned buckets report EMPTY output; Enabled writes get fresh version
#     ids, Suspended / never-versioned ones the "null" version (replacing the old null one);
#   * lifecycle: PUT validates like S3 (a noncurrent move must come before the expiry, newest-N
#     1..100, days >= 1, IDs unique, one action at least) and GET hands the rules back in S3's own
#     key order plus TransitionDefaultMinimumObjectSize; NoSuchLifecycleConfiguration when none;
#   * list-object-versions: key order, newest version first, MaxKeys counting versions AND delete
#     markers, KeyMarker/VersionIdMarker paging, LastModified at S3's whole-second resolution;
#   * delete-bucket refuses a bucket with any version or delete marker left (BucketNotEmpty);
#   * optional read-after-write lag: the next `lag_reads` GETs after a config put return the
#     previous configuration (S3 bucket configuration is eventually consistent).
# It also polices the test: every call must carry exactly the admin credentials (or none at all
# for `--version` / `help`), and any mutating call on a live bucket is refused and recorded.
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_GLOBAL_WITH_VALUE = {"--cli-connect-timeout", "--cli-read-timeout", "--output", "--region", "--query",
                      "--profile", "--endpoint-url"}
_GLOBAL_FLAGS = {"--no-paginate", "--no-cli-pager", "--debug"}
_S3_RULE_ORDER = ("Expiration", "ID", "Prefix", "Filter", "Status", "Transitions",
                  "NoncurrentVersionTransitions", "NoncurrentVersionExpiration", "AbortIncompleteMultipartUpload")
_READ_OPS = {"get-bucket-lifecycle-configuration", "get-bucket-versioning", "get-bucket-location", "head-bucket",
             "list-object-versions"}
_CLASSES = {"STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING", "GLACIER", "DEEP_ARCHIVE", "GLACIER_IR"}
MIN_SIZE_FLAG = "--transition-default-minimum-object-size"
AWS_VERSION = "aws-cli/2.15.57 Python/3.12.3 Linux/6.6.0 source/x86_64.alpine.3 prompt/off"


def _ok(stdout="") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _api(op: str) -> str:
    return "".join(p.capitalize() for p in op.split("-"))


def _err(code: str, op: str, msg: str, rc: int = 254) -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout="",
                           stderr=f"\nAn error occurred ({code}) when calling the {_api(op)} operation: {msg}\n")


def _lm(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _Bucket:
    def __init__(self, region: str, versioning=None, lifecycle=None):
        self.region = region
        self.versioning = versioning          # None (never) | "Enabled" | "Suspended"
        self.lifecycle = lifecycle            # None | list of rules
        self.min_size = "all_storage_classes_128K"
        self.objects: dict[str, list[dict]] = {}   # key -> versions/markers, NEWEST FIRST
        self.lag = {"lifecycle": [0, None], "versioning": [0, None]}


class FakeAWS:
    def __init__(self, *, region: str, creds: dict, live: dict | None = None, help_has_flag: bool = False,
                 lag_reads: int = 0, fail=None, interrupt=None, clock=None, mangle_get=None):
        self.region = region
        self.creds = dict(creds)
        self.help_has_flag = help_has_flag
        self.lag_reads = lag_reads
        self.fail = fail                      # (op, bucket, args) -> (code, message) | None
        self.interrupt = interrupt            # (op, bucket, args) -> bool: raise KeyboardInterrupt once
        self.clock = clock or (lambda: datetime.now(timezone.utc).replace(microsecond=0))
        self.mangle_get = mangle_get          # rule -> rule: S3 answering in a form the app didn't write
        self.buckets: dict[str, _Bucket] = {}
        self.live = set()
        for name, spec in (live or {}).items():
            self.buckets[name] = _Bucket(spec.get("region", region), spec.get("versioning"),
                                         json.loads(json.dumps(spec["lifecycle"])) if spec.get("lifecycle") else None)
            self.live.add(name)
        self.initial_live = {n: self._snapshot(n) for n in self.live}
        self.calls: list[dict] = []
        self.cred_errors: list[list] = []
        self.live_mutations: list[list] = []
        self.unknown: list[list] = []
        self.created: list[str] = []
        self.deleted: list[str] = []

    # --- helpers for the tests -------------------------------------------------------------------

    def _snapshot(self, name):
        b = self.buckets[name]
        return json.dumps({"v": b.versioning, "l": b.lifecycle, "o": b.objects}, sort_keys=True, default=str)

    def live_unchanged(self) -> bool:
        return all(n in self.buckets and self._snapshot(n) == s for n, s in self.initial_live.items())

    def ops(self, op: str) -> list[dict]:
        return [c for c in self.calls if c["op"] == op]

    # --- the call --------------------------------------------------------------------------------

    def __call__(self, args, *, region, key, secret, session_token=None):
        args = list(args)
        pos, opts, flags = self._parse(args)
        service = pos[0] if pos else None
        op = pos[1] if len(pos) > 1 else (args[0] if args else None)
        bucket = opts.get("--bucket")
        if bucket is None and "--cli-input-json" in opts:
            try:
                bucket = json.loads(opts["--cli-input-json"]).get("Bucket")
            except ValueError:
                bucket = None
        self.calls.append({"args": args, "op": op, "bucket": bucket, "anon": not key,
                           "token": session_token})

        if args == ["--version"]:
            if key or secret or session_token:
                self.cred_errors.append(args)
            return SimpleNamespace(returncode=0, stdout=AWS_VERSION + "\n", stderr="")
        if service == "s3api" and len(pos) >= 3 and pos[2] == "help":
            if key or secret or session_token:
                self.cred_errors.append(args)
            text = f"{op}\n\nDESCRIPTION\n  Creates a new lifecycle configuration ...\n\nSYNOPSIS\n  {op}\n  --bucket <value>\n"
            if self.help_has_flag:
                text += f"  [{MIN_SIZE_FLAG} <value>]\n\nOPTIONS\n  {MIN_SIZE_FLAG} (string)\n"
            return _ok(text)
        want_token = self.creds.get("AWS_SESSION_TOKEN") or None
        if (key, secret, session_token or None) != (self.creds["AWS_ACCESS_KEY_ID"],
                                                    self.creds["AWS_SECRET_ACCESS_KEY"], want_token):
            self.cred_errors.append(args)
            return _err("InvalidAccessKeyId", op or "?", "The AWS Access Key Id you provided does not exist in our records.")
        if service != "s3api":
            self.unknown.append(args)
            return _err("InvalidAction", op or "?", "fake: not an s3api call")
        if bucket in self.live and op not in _READ_OPS:
            self.live_mutations.append(args)
            return _err("AccessDenied", op, "fake: a live bucket is read-only in this test")
        if self.interrupt and self.interrupt(op, bucket, args):
            self.interrupt = None
            raise KeyboardInterrupt
        if self.fail:
            f = self.fail(op, bucket, args)
            if f:
                return _err(f[0], op, f[1])
        handler = getattr(self, "_" + op.replace("-", "_"), None)
        if handler is None:
            self.unknown.append(args)
            return _err("InvalidAction", op, "fake: unsupported call")
        if op not in ("create-bucket",) and bucket not in self.buckets:
            return _err("NoSuchBucket", op, "The specified bucket does not exist")
        return handler(bucket, opts, flags, args)

    @staticmethod
    def _parse(args):
        pos, opts, flags, i = [], {}, set(), 0
        while i < len(args):
            a = args[i]
            if a in _GLOBAL_FLAGS:
                flags.add(a)
                i += 1
            elif a.startswith("--"):
                if i + 1 < len(args) and not args[i + 1].startswith("--"):
                    opts[a] = args[i + 1]
                    i += 2
                else:
                    flags.add(a)
                    i += 1
            else:
                pos.append(a)
                i += 1
        return pos, opts, flags

    # --- buckets ---------------------------------------------------------------------------------

    def _create_bucket(self, bucket, opts, flags, args):
        if bucket in self.buckets:
            return _err("BucketAlreadyOwnedByYou", "create-bucket", "Your previous request to create the named "
                        "bucket succeeded and you already own it.", rc=254)
        conf = opts.get("--create-bucket-configuration")
        region = opts.get("--region", self.region)
        if region == "us-east-1" and conf:
            return _err("InvalidLocationConstraint", "create-bucket", "The specified location-constraint is not valid")
        if region != "us-east-1" and conf != f"LocationConstraint={region}":
            return _err("IllegalLocationConstraintException", "create-bucket",
                        "The unspecified location constraint is incompatible for the region specific endpoint "
                        "this request was sent to.")
        self.buckets[bucket] = _Bucket(region)
        self.created.append(bucket)
        return _ok(json.dumps({"Location": f"/{bucket}" if region == "us-east-1" else
                               f"http://{bucket}.s3.amazonaws.com/"}))

    def _head_bucket(self, bucket, opts, flags, args):
        return _ok("")

    def _get_bucket_location(self, bucket, opts, flags, args):
        r = self.buckets[bucket].region
        return _ok(json.dumps({"LocationConstraint": None if r == "us-east-1" else r}))

    def _delete_bucket(self, bucket, opts, flags, args):
        b = self.buckets[bucket]
        if any(b.objects.values()):
            return _err("BucketNotEmpty", "delete-bucket", "The bucket you tried to delete is not empty. "
                        "You must delete all versions in the bucket.", rc=254)
        del self.buckets[bucket]
        self.deleted.append(bucket)
        return _ok("")

    # --- versioning ------------------------------------------------------------------------------

    def _lagged(self, b: _Bucket, kind: str, current):
        left, previous = b.lag[kind]
        if left > 0:
            b.lag[kind][0] = left - 1
            return previous
        return current

    def _set_lag(self, b: _Bucket, kind: str, previous):
        if self.lag_reads:
            b.lag[kind] = [self.lag_reads, json.loads(json.dumps(previous))]

    def _put_bucket_versioning(self, bucket, opts, flags, args):
        conf = opts.get("--versioning-configuration", "")
        status = conf.split("=", 1)[1] if conf.startswith("Status=") else None
        if status not in ("Enabled", "Suspended"):
            return _err("MalformedXML", "put-bucket-versioning", "The XML you provided was not well-formed")
        b = self.buckets[bucket]
        self._set_lag(b, "versioning", b.versioning)
        b.versioning = status
        return _ok("")

    def _get_bucket_versioning(self, bucket, opts, flags, args):
        b = self.buckets[bucket]
        v = self._lagged(b, "versioning", b.versioning)
        return _ok(json.dumps({"Status": v}) if v else "")        # never versioned: EMPTY output

    # --- lifecycle -------------------------------------------------------------------------------

    def _put_bucket_lifecycle_configuration(self, bucket, opts, flags, args):
        if MIN_SIZE_FLAG in opts and not self.help_has_flag:
            return SimpleNamespace(returncode=252, stdout="",
                                   stderr=f"\nUnknown options: {MIN_SIZE_FLAG}, {opts[MIN_SIZE_FLAG]}\n")
        try:
            rules = json.loads(opts["--lifecycle-configuration"])["Rules"]
        except (KeyError, ValueError, TypeError):
            return _err("MalformedXML", "put-bucket-lifecycle-configuration", "The XML you provided was not well-formed")
        problem = _validate(rules)
        if problem:
            return _err(problem[0], "put-bucket-lifecycle-configuration", problem[1])
        b = self.buckets[bucket]
        self._set_lag(b, "lifecycle", b.lifecycle)
        b.lifecycle = json.loads(json.dumps(rules))
        if MIN_SIZE_FLAG in opts:
            b.min_size = opts[MIN_SIZE_FLAG]
        return _ok("")

    def _get_bucket_lifecycle_configuration(self, bucket, opts, flags, args):
        b = self.buckets[bucket]
        rules = self._lagged(b, "lifecycle", b.lifecycle)
        if not rules:
            return _err("NoSuchLifecycleConfiguration", "get-bucket-lifecycle-configuration",
                        "The lifecycle configuration does not exist")
        out = [{k: r[k] for k in _S3_RULE_ORDER if k in r} for r in json.loads(json.dumps(rules))]
        if self.mangle_get:
            out = [self.mangle_get(r) for r in out]
        return _ok(json.dumps({"TransitionDefaultMinimumObjectSize": b.min_size, "Rules": out}, indent=4))

    def _delete_bucket_lifecycle(self, bucket, opts, flags, args):
        self.buckets[bucket].lifecycle = None
        return _ok("")

    # --- objects ---------------------------------------------------------------------------------

    def _new_vid(self, b: _Bucket) -> str:
        return secrets.token_urlsafe(24) if b.versioning == "Enabled" else "null"

    def _add(self, b: _Bucket, key: str, entry: dict) -> None:
        versions = b.objects.setdefault(key, [])
        if entry["VersionId"] == "null":
            versions[:] = [v for v in versions if v["VersionId"] != "null"]
        versions.insert(0, entry)

    def _put_object(self, bucket, opts, flags, args):
        b = self.buckets[bucket]
        body = Path(opts["--body"]).read_bytes() if "--body" in opts else b""
        vid = self._new_vid(b)
        self._add(b, opts["--key"], {"VersionId": vid, "LastModified": self.clock(), "Size": len(body),
                                     "marker": False})
        out = {"ETag": f'"{secrets.token_hex(16)}"'}
        if b.versioning:
            out["VersionId"] = vid
        return _ok(json.dumps(out))

    def _remove_version(self, b: _Bucket, key: str, vid: str):
        versions = b.objects.get(key) or []
        hit = next((v for v in versions if v["VersionId"] == vid), None)
        if hit is not None:
            versions.remove(hit)
            if not versions:
                b.objects.pop(key, None)
        return hit

    def _delete_one(self, b: _Bucket, key: str, vid: str | None) -> dict:
        if vid:
            hit = self._remove_version(b, key, vid)
            out = {"VersionId": vid}
            if hit is not None and hit["marker"]:
                out["DeleteMarker"] = True
            return out
        if b.versioning is None:
            b.objects.pop(key, None)
            return {}
        mvid = self._new_vid(b)
        self._add(b, key, {"VersionId": mvid, "LastModified": self.clock(), "Size": 0, "marker": True})
        return {"DeleteMarker": True, "VersionId": mvid}

    def _delete_object(self, bucket, opts, flags, args):
        return _ok(json.dumps(self._delete_one(self.buckets[bucket], opts["--key"], opts.get("--version-id"))))

    def _delete_objects(self, bucket, opts, flags, args):
        spec = json.loads(opts["--delete"])
        b = self.buckets[bucket]
        deleted = [dict(self._delete_one(b, o["Key"], o.get("VersionId")), Key=o["Key"]) for o in spec["Objects"]]
        return _ok(json.dumps({} if spec.get("Quiet") else {"Deleted": deleted}))

    def _list_object_versions(self, bucket, opts, flags, args):
        inp = json.loads(opts["--cli-input-json"]) if "--cli-input-json" in opts else {}
        prefix = inp.get("Prefix", opts.get("--prefix", ""))
        max_keys = int(inp.get("MaxKeys", opts.get("--max-keys", 1000)))
        km, vm = inp.get("KeyMarker"), inp.get("VersionIdMarker")
        b = self.buckets[bucket]
        rows = []
        for k in sorted(b.objects):
            if not k.startswith(prefix):
                continue
            for i, v in enumerate(b.objects[k]):
                rows.append((k, v, i == 0))
        start = 0
        if km is not None:
            if vm:
                idx = next((i for i, (k, v, _) in enumerate(rows) if k == km and v["VersionId"] == vm), None)
                start = idx + 1 if idx is not None else next((i for i, (k, _, _) in enumerate(rows) if k > km), len(rows))
            else:
                start = next((i for i, (k, _, _) in enumerate(rows) if k > km), len(rows))
        page = rows[start:start + max_keys]
        truncated = start + max_keys < len(rows)
        out = {"IsTruncated": truncated, "Name": bucket, "Prefix": prefix, "MaxKeys": max_keys}
        vs = [{"ETag": '"x"', "Size": v["Size"], "StorageClass": "STANDARD", "Key": k, "VersionId": v["VersionId"],
               "IsLatest": latest, "LastModified": _lm(v["LastModified"])} for k, v, latest in page if not v["marker"]]
        ms = [{"Key": k, "VersionId": v["VersionId"], "IsLatest": latest, "LastModified": _lm(v["LastModified"])}
              for k, v, latest in page if v["marker"]]
        if vs:
            out["Versions"] = vs
        if ms:
            out["DeleteMarkers"] = ms
        if truncated:
            out["NextKeyMarker"], out["NextVersionIdMarker"] = page[-1][0], page[-1][1]["VersionId"]
        return _ok(json.dumps(out))


def _validate(rules) -> tuple[str, str] | None:
    """What S3 refuses in a lifecycle configuration (the parts the app's rules can touch)."""
    if not isinstance(rules, list) or not rules or len(rules) > 1000:
        return "MalformedXML", "The XML you provided was not well-formed or did not validate against our published schema"
    ids = [r.get("ID") for r in rules]
    if len(set(ids)) != len(ids):
        return "InvalidArgument", "Rule ID must be unique. Found same ID for more than one rule"
    for r in rules:
        if not isinstance(r.get("ID"), str) or len(r["ID"]) > 255:
            return "InvalidArgument", "ID length should not exceed allowed limit of 255"
        if r.get("Status") not in ("Enabled", "Disabled"):
            return "MalformedXML", "Status must be Enabled or Disabled"
        if ("Filter" in r) == ("Prefix" in r):
            return "MalformedXML", "Exactly one of Filter or Prefix is required"
        actions = [k for k in ("Expiration", "Transitions", "NoncurrentVersionTransitions",
                               "NoncurrentVersionExpiration", "AbortIncompleteMultipartUpload") if k in r]
        if not actions:
            return "InvalidRequest", "At least one action needs to be specified in a rule"
        exp = r.get("Expiration")
        if exp is not None:
            if "ExpiredObjectDeleteMarker" in exp and ("Days" in exp or "Date" in exp):
                return "MalformedXML", "ExpiredObjectDeleteMarker cannot be specified with Days or Date"
            if "Days" in exp and (not isinstance(exp["Days"], int) or exp["Days"] < 1):
                return "InvalidArgument", "'Days' for Expiration action must be a positive integer"
        nce = r.get("NoncurrentVersionExpiration")
        if nce is not None:
            d = nce.get("NoncurrentDays")
            if not isinstance(d, int) or d < 1:
                return "InvalidArgument", "'NoncurrentDays' for NoncurrentVersionExpiration action must be a positive integer"
            n = nce.get("NewerNoncurrentVersions")
            if n is not None and (not isinstance(n, int) or not 1 <= n <= 100):
                return "InvalidArgument", "NewerNoncurrentVersions must be between 1 and 100"
        for t in r.get("NoncurrentVersionTransitions") or []:
            d, cls = t.get("NoncurrentDays"), t.get("StorageClass")
            if cls not in _CLASSES or not isinstance(d, int) or d < 0:
                return "InvalidArgument", "Invalid NoncurrentVersionTransition"
            if cls in ("STANDARD_IA", "ONEZONE_IA") and d < 30:
                return "InvalidArgument", f"'NoncurrentDays' in NoncurrentVersionTransition action for StorageClass '{cls}' must be 30 or greater"
            if nce is not None and d >= nce["NoncurrentDays"]:
                return "InvalidArgument", ("'NoncurrentDays' in NoncurrentVersionExpiration action must be later than "
                                           f"'NoncurrentDays' in NoncurrentVersionTransition action for StorageClass '{cls}'")
        abort = r.get("AbortIncompleteMultipartUpload")
        if abort is not None and (not isinstance(abort.get("DaysAfterInitiation"), int) or abort["DaysAfterInitiation"] < 1):
            return "InvalidArgument", "'DaysAfterInitiation' must be a positive integer"
    return None
