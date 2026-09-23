# app/engine/storage_summary.py — per-folder storage summary (spec 2026-09-23 §4, R-B8). A
# read-only scan of one app folder's object versions with the RUNTIME key (ListBucketVersions
# -- never the bucket-admin role), stored as histograms in
# state/storage/<bucket>__<folder-slug>.json, so an S3 rules preview can say what a rule
# would remove without listing the bucket again. `impact` is pure and exact with respect
# to the stored summary.
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import lifecycle
from ..gui import provision

RANK_CAP = 101          # ranks above S3's NewerNoncurrentVersions maximum (100) fold into 101
PAGE_KEYS = 1000        # versions per ListObjectVersions page (S3's maximum)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class SummaryError(Exception):
    """A scan failed; the message is secret-scrubbed."""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def slug(folder: str) -> str:
    return folder.strip("/").replace("/", "-") or "bucket"


def summary_path(cache_dir, bucket: str, folder: str) -> Path:
    return Path(cache_dir, "state", "storage", f"{bucket}__{slug(folder)}.json")


def load(cache_dir, bucket: str, folder: str) -> dict | None:
    try:
        data = json.loads(summary_path(cache_dir, bucket, folder).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(cache_dir, summary: dict) -> None:
    p = summary_path(cache_dir, summary["bucket"], summary["folder"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(summary))
    os.replace(tmp, p)


class _Tally:
    def __init__(self, now: datetime):
        self.now = now
        self.by_age: dict = {}
        self.by_rank: dict = {}
        self.joint: dict = {}
        self.markers = self.current = self.current_bytes = self.versions = self.bytes = 0

    @staticmethod
    def _add(d: dict, k, size: int) -> None:
        n, b = d.get(k, (0, 0))
        d[k] = (n + 1, b + size)

    def key(self, entries: list[tuple]) -> None:
        """One file's versions and delete markers, newest first (the current one leads).
        age = whole days since the version was replaced (its successor's LastModified);
        rank = 1 for the newest old version, 2 the next, ... (delete markers are not ranked)."""
        entries = sorted(entries, key=lambda e: (not e[3], -(e[1] or _EPOCH).timestamp()))
        successor, rank = None, 0
        for i, (_k, lm, marker, latest, size) in enumerate(entries):
            if marker:
                self.markers += 1
            elif i == 0 and latest:
                self.current += 1
                self.current_bytes += size
            else:
                rank += 1
                replaced = successor or lm or self.now
                age = max(0, int((self.now - replaced).total_seconds() // 86400))
                r = min(rank, RANK_CAP)
                self._add(self.by_age, age, size)
                self._add(self.by_rank, r, size)
                self._add(self.joint, (age, r), size)
                self.versions += 1
                self.bytes += size
            successor = lm

    def summary(self, bucket: str, folder: str) -> dict:
        def rows(d):
            return [[k, n, b] for k, (n, b) in sorted(d.items())]
        return {"v": 1, "scanned_at": _iso(self.now), "bucket": bucket, "folder": folder,
                "noncurrent_by_age_days": rows(self.by_age),
                "noncurrent_by_rank": rows(self.by_rank),
                "noncurrent_by_age_rank": [[a, r, n, b] for (a, r), (n, b) in sorted(self.joint.items())],
                "noncurrent_versions": self.versions, "noncurrent_bytes": self.bytes,
                "delete_markers": self.markers, "current_objects": self.current,
                "current_bytes": self.current_bytes}


def _entries(page: dict) -> list[tuple]:
    out = []
    for v in page.get("Versions") or []:
        out.append((v.get("Key", ""), _parse(v.get("LastModified")), False, bool(v.get("IsLatest")),
                    int(v.get("Size") or 0)))
    for m in page.get("DeleteMarkers") or []:
        out.append((m.get("Key", ""), _parse(m.get("LastModified")), True, bool(m.get("IsLatest")), 0))
    return out


def scan(bucket: str, folder: str, *, region: str, key: str, secret: str, run=provision._run_aws,
         now: datetime | None = None, log=None, page_keys: int = PAGE_KEYS) -> dict:
    """List every version under `folder` ("" = the whole bucket), one S3 page at a time.
    S3 pages are cut at a (key, version) boundary, so every key before NextKeyMarker is
    complete and only that key is carried into the next page."""
    now = now or datetime.now(timezone.utc)
    log = log or (lambda msg: None)
    tally, carry, marker, pages = _Tally(now), [], None, 0
    while True:
        inp = {"Bucket": bucket, "MaxKeys": int(page_keys)}
        if folder:
            inp["Prefix"] = folder
        if marker:
            inp["KeyMarker"], inp["VersionIdMarker"] = marker
        cp = run(["s3api", "list-object-versions", "--no-paginate", "--cli-input-json", json.dumps(inp),
                  "--output", "json"], region=region, key=key, secret=secret)
        if cp.returncode != 0:
            raise SummaryError(provision._scrub(cp.stderr or "", key, secret).strip()
                               or "listing the old versions failed")
        try:
            page = json.loads(cp.stdout or "{}")
        except ValueError:
            raise SummaryError("unreadable version listing")
        pages += 1
        truncated = bool(page.get("IsTruncated"))
        next_key = page.get("NextKeyMarker")
        by_key: dict[str, list] = {}
        for e in carry + _entries(page):
            by_key.setdefault(e[0], []).append(e)
        carry = []
        for k, es in by_key.items():
            if truncated and k == next_key:
                carry = es                      # this file may continue on the next page
            else:
                tally.key(es)
        if pages % 25 == 0:
            log(f"storage summary: {tally.versions + tally.current:,} versions listed so far")
        if not truncated:
            break
        if not next_key:
            raise SummaryError("the version listing stopped without saying where to continue")
        marker = (next_key, page.get("NextVersionIdMarker") or "null")
    return tally.summary(bucket, folder)


def impact(summary: dict, old_rule, new_rule) -> dict:
    """What `new_rule` removes that `old_rule` keeps, per the stored summary: {"versions",
    "bytes", "oldest_age_days"} (days since the oldest affected version was replaced; None
    when nothing). A version goes when replaced >= NoncurrentDays ago AND ranked beyond
    NewerNoncurrentVersions -- the same reading as lifecycle.expiry."""
    od, on = lifecycle.expiry(old_rule)
    nd, nn = lifecycle.expiry(new_rule)
    versions = size = 0
    oldest = None
    for age, rank, n, b in summary.get("noncurrent_by_age_rank") or []:
        if age >= nd and rank > nn and not (age >= od and rank > on):
            versions += n
            size += b
            oldest = age if oldest is None else max(oldest, age)
    return {"versions": versions, "bytes": size, "oldest_age_days": oldest}
