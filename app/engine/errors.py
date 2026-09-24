# app/engine/errors.py — map a failed run to one owner-facing error class (spec §7.4).
#
# One failure has to be said at three lengths: `verdict` (Board band, one line),
# `cause` (the WHY THIS HAPPENS paragraph on the job page / run record), and
# `board` (the needs-you row body — the only one that knows the schedule, hence
# the {dow}/{since} placeholders status.py fills). `classify` matches on the run's
# `error` field first, then the last 200 lines of the run log.
from __future__ import annotations

import dataclasses
import re


@dataclasses.dataclass(frozen=True)
class ErrorClass:
    code: str
    short: str
    verdict: str       # ONE line, Board band 1's second sentence (5.1)
    cause: str         # the full "WHY THIS HAPPENS" paragraph (job page, run record)
    board: str | None  # needs-you row body; may contain {dow} and {since}
    fix: str
    fix_label: str | None
    fix_route: str | None
    blocker: bool


CLASSES: dict[str, ErrorClass] = {
    # AccessDenied on a version delete/listing. The key no longer deletes old versions at
    # all (spec 2026-09-23: the bucket's S3 rules remove them), so this never asks the
    # owner to grant that back -- it points at the permissions update, which brings the
    # key to what this version uses (e.g. the version listing restores and scans need).
    "iam-version-perms": ErrorClass(
        code="iam-version-perms",
        short="AccessDenied",
        verdict="Amazon refused a delete or a version listing — the key this machine uses doesn't allow it.",
        cause=("Amazon refused to delete or list old versions of files. Old versions are removed by the "
               "bucket's S3 rules now, never by the key this machine uses, so a refused delete of an old "
               "version comes from an earlier version of backup-engine and won't come back. A refused "
               "listing means the key's permissions are out of date."),
        board=("Amazon refused a delete or a version listing, so every {dow} run stops at the same point "
               "(since {since}). Old versions are removed by the bucket's S3 rules now — bring the key's "
               "permissions up to date."),
        fix=("Open Setup → AWS permissions and update them: that brings the key this machine uses to "
             "what this version of backup-engine needs. It never needs to delete old versions itself — "
             "the bucket's S3 rules do that."),
        fix_label="Fix the permission →",
        fix_route="/setup/permissions",
        blocker=True,
    ),
    "access-denied": ErrorClass(
        code="access-denied",
        short="AccessDenied",
        verdict="Amazon refused a request — one permission is missing from the key this machine uses.",
        cause="Amazon refused a request — the key this machine uses lacks a permission for it.",
        board=None,
        fix="Re-apply the key policy with ./setup.sh, or compare the key's policy with the one Setup shows.",
        fix_label="Check the key →",
        fix_route="/setup/destination",
        blocker=True,
    ),
    "repo-locked": ErrorClass(
        code="repo-locked",
        short="store locked",
        verdict="Two snapshot backups tried to use the store in the same minute.",
        cause="Two snapshot backups tried to use the store at the same minute, and the second found it locked.",
        board=None,
        fix="Give the two jobs different minutes. Nothing is damaged; this job runs normally next time.",
        fix_label="Edit the schedule →",
        fix_route="/jobs/<name>/edit",
        blocker=False,
    ),
    "wrong-passphrase": ErrorClass(
        code="wrong-passphrase",
        short="wrong passphrase",
        verdict="The recovery passphrase on this machine does not open the snapshot store.",
        cause="The recovery passphrase this machine has does not open the snapshot store.",
        board=None,
        fix=("Enter the passphrase that was used when the store was created, under Keys & secrets. "
             "Without it no snapshot can be read."),
        fix_label="Set it →",
        fix_route="/setup/keys#RESTIC_PASSWORD",
        blocker=True,
    ),
    "cold-object": ErrorClass(
        code="cold-object",
        short="not warmed up",
        verdict="The files are still cold — Amazon has to warm them up before anything can read them.",
        cause="These files are on a thaw-first tier and have not been warmed up.",
        board=None,
        fix="Warm up first, wait the stated hours, then download again.",
        fix_label="Warm up →",
        fix_route="/jobs/<name>#restore-band",
        blocker=False,
    ),
    "source-missing": ErrorClass(
        code="source-missing",
        short="source missing",
        verdict="The folder this job protects was not there when the run started.",
        cause=("The folder this job protects was not there when the run started — usually the share was "
               "not mounted yet."),
        board=None,
        fix="Check the path mapping for the container and that the share exists; then Run now.",
        fix_label="Edit the job →",
        fix_route="/jobs/<name>/edit",
        blocker=True,
    ),
    "no-space": ErrorClass(
        code="no-space",
        short="disk full",
        verdict="The disk this job writes to is full.",
        cause="The cache or restore disk is full.",
        board=None,
        fix="Free space under /cache (restic cache) or the restore folder, then Run now.",
        fix_label=None,
        fix_route=None,
        blocker=False,
    ),
    "killed": ErrorClass(
        code="killed",
        short="stopped",
        verdict="The run was stopped from outside — the container restarted or was stopped.",
        cause="The run was stopped from outside — the container restarted or was stopped.",
        board=None,
        fix="Nothing to fix if you restarted it on purpose; otherwise check the container's log.",
        fix_label=None,
        fix_route=None,
        blocker=False,
    ),
    "busy": ErrorClass(
        code="busy",
        short="busy",
        verdict="Another operation on this job was still running.",
        cause="Started while another operation on this job was still running.",
        board=None,
        fix="Wait for the running operation; this one will run at its next scheduled time.",
        fix_label=None,
        fix_route=None,
        blocker=False,
    ),
    "unknown": ErrorClass(
        code="unknown",
        short="error",
        verdict="The run stopped with an error; open the record to see it.",
        cause="The tool reported an error this app does not recognise.",
        board=None,
        fix="Read the log below; the last lines usually name the reason.",
        fix_label=None,
        fix_route=None,
        blocker=False,
    ),
}

_VERSION_VERBS = ("DeleteObjectVersion", "ListBucketVersions", "list-object-versions", "delete-object")


def _text_class(text: str) -> str | None:
    """The first text-based §7.4 class whose pattern is present in `text` (in
    table order). Signal-based classes (killed) are handled by classify()."""
    if not text:
        return None
    if "AccessDenied" in text and any(v in text for v in _VERSION_VERBS):
        return "iam-version-perms"
    if "AccessDenied" in text or "403" in text:
        return "access-denied"
    if "repository is already locked" in text or "unable to create lock" in text:
        return "repo-locked"
    if "wrong password" in text or "no key could be found" in text:
        return "wrong-passphrase"
    if "InvalidObjectState" in text or "not valid for the object's storage class" in text:
        return "cold-object"
    if re.search(r"source .* missing", text) or re.search(r"source root .* not found", text):
        return "source-missing"
    if "no space left on device" in text:
        return "no-space"
    if re.search(r"another .* run is in progress", text):
        return "busy"
    return None


def _last_lines(text: str, n: int) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-n:])


def _unknown_short(error: str | None, log_tail: str) -> str:
    src = (error or "").strip()
    if not src:
        for line in reversed((log_tail or "").splitlines()):
            if line.strip():
                src = line.strip()
                break
    if not src:
        return "error"
    return src.split(":")[0].strip()[:24] or "error"


def classify(error: str | None, exit_code: int | None, outcome: str | None,
             log_tail: str = "") -> ErrorClass | None:
    # 1) the run's error field is authoritative — try it first.
    code = _text_class(error or "")
    if code:
        return CLASSES[code]
    # 2) killed is signal-based (a kill/restart leaves no useful message).
    if exit_code in (137, 143) or outcome == "aborted":
        return CLASSES["killed"]
    # 3) fall back to the last 200 lines of the run log.
    code = _text_class(_last_lines(log_tail, 200))
    if code:
        return CLASSES[code]
    # 4) nothing to classify -> a healthy or still-running record.
    failed_signal = bool(error) or outcome == "failed" or (exit_code not in (0, None))
    if not failed_signal:
        return None
    # 5) an unrecognised failure.
    return dataclasses.replace(CLASSES["unknown"], short=_unknown_short(error, log_tail))
