import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.gui import points
from app.engine import runs
from app.engine import catalog


def _write_points(cache, job, obj):
    p = Path(cache, "state", f"{job}.points.json")
    p.write_text(json.dumps(obj))
    return p


def _snap(short_id, time, *, summary=True, tbp=52_000_000_000, tfp=533, fn=2, fc=4, da=228_000_000):
    d = {"id": short_id + "0" * (64 - len(short_id)), "short_id": short_id, "time": time,
         "tags": ["appdata"], "hostname": "h"}
    if summary:
        d["summary"] = {"total_bytes_processed": tbp, "total_files_processed": tfp,
                        "files_new": fn, "files_changed": fc, "data_added": da}
    return d


def _seed_ok_backup(cache, job, run_id, finished_at, *, kind="backup", type_="versioned",
                    files_added=None, bytes_added=None):
    started = finished_at
    runs.append_event(cache, job, {"v": 1, "id": run_id, "job": job, "kind": kind,
                                   "event": "start", "started_at": started, "type": type_})
    end = {"v": 1, "id": run_id, "job": job, "kind": kind, "event": "end", "outcome": "ok",
           "finished_at": finished_at, "duration_s": 10, "exit_code": 0}
    if files_added is not None:
        end["files_added"] = files_added
    if bytes_added is not None:
        end["bytes_added"] = bytes_added
    runs.append_event(cache, job, end)


# --- versioned (Snapshot backup) ------------------------------------------

def test_versioned_newest_six_plus_oldest_when_count_ge_8(dirs):
    cache = dirs["cache"]
    days = [f"2026-09-{d:02d}T05:00:01Z" for d in range(1, 15)]  # 14 points, oldest first
    snaps = [_snap(f"id{d:02d}aa", t) for d, t in enumerate(days, start=1)]
    _write_points(cache, "appdata", snaps)
    v = points.view(cache, {"name": "appdata", "type": "versioned", "storage_class": "STANDARD"})
    assert v["kind"] == "snapshots"
    assert v["count"] == 14
    assert v["cold"] is False
    # newest first
    assert v["points"][0]["time"].startswith("2026-09-14")
    assert v["points"][-1]["time"].startswith("2026-09-01")
    # visible = newest six + the oldest = 7 rows
    assert len(v["visible"]) == 7
    assert v["visible"][-1]["time"].startswith("2026-09-01")   # the oldest
    assert v["visible"][0]["time"].startswith("2026-09-14")    # the newest
    assert v["has_details"] is True
    assert v["hidden_count"] == 7                              # count - 7
    assert len(v["hidden"]) == 7
    assert v["default_point"] == v["points"][0]["id"]
    assert v["size_provenance"] == "measured"


def test_versioned_all_rows_when_count_le_7(dirs):
    cache = dirs["cache"]
    days = [f"2026-09-{d:02d}T05:00:01Z" for d in range(1, 6)]  # 5 points
    _write_points(cache, "appdata", [_snap(f"id{d}", t) for d, t in enumerate(days, 1)])
    v = points.view(cache, {"name": "appdata", "type": "versioned", "storage_class": "STANDARD"})
    assert v["count"] == 5
    assert len(v["visible"]) == 5
    assert v["hidden"] == []
    assert v["has_details"] is False
    assert v["hidden_count"] == 0


def test_versioned_point_without_summary_reports_size_not_recorded(dirs):
    cache = dirs["cache"]
    snaps = [_snap("newer", "2026-09-14T05:00:01Z", summary=True),
             _snap("older", "2026-09-13T05:00:01Z", summary=False)]
    _write_points(cache, "appdata", snaps)
    v = points.view(cache, {"name": "appdata", "type": "versioned", "storage_class": "STANDARD"})
    by_id = {p["id"]: p for p in v["points"]}
    assert by_id["older"]["summary"] is False
    assert by_id["older"]["size_bytes"] is None
    assert by_id["newer"]["summary"] is True
    assert by_id["newer"]["size_bytes"] == 52_000_000_000


def test_versioned_rfc3339_nanoseconds_and_offset_parse(dirs):
    cache = dirs["cache"]
    _write_points(cache, "appdata", [_snap("nano", "2026-09-15T05:00:01.123456789+00:00")])
    v = points.view(cache, {"name": "appdata", "type": "versioned", "storage_class": "STANDARD"})
    assert v["count"] == 1
    assert v["points"][0]["time"].startswith("2026-09-15T05:00:01")
    assert v["points"][0]["label"] == "Tue 15 Sep 05:00"


def test_versioned_cold_class_flag(dirs):
    cache = dirs["cache"]
    _write_points(cache, "appdata", [_snap("a", "2026-09-15T05:00:01Z")])
    v = points.view(cache, {"name": "appdata", "type": "versioned", "storage_class": "DEEP_ARCHIVE"})
    assert v["cold"] is True


# --- archive (Plain copy) --------------------------------------------------

def test_archive_what_is_there_now(dirs):
    cache = dirs["cache"]
    _write_points(cache, "movies", {"kind": "current-copy", "folders": ["2019", "2020"],
                                    "as_of": "2026-09-06T06:09:00Z"})
    Path(cache, "usage.json").write_text(json.dumps(
        {"fetched_at": datetime(2026, 9, 14, 7, 40, tzinfo=timezone.utc).timestamp(),
         "data": {"media/movies": {"bytes": 1_957_000_000_000, "count": 232021}}}))
    _seed_ok_backup(cache, "movies", "20260906T060900Z-aa11", "2026-09-06T06:09:00Z",
                    type_="archive")
    v = points.view(cache, {"name": "movies", "type": "archive", "storage_class": "DEEP_ARCHIVE",
                            "mirror": False})
    assert v["kind"] == "current-copy"
    assert v["folders"] == ["2019", "2020"]
    assert v["size_bytes"] == 1_957_000_000_000
    assert v["file_count"] == 232021
    assert v["size_provenance"] == "measured"
    assert v["cold"] is True
    assert v["note"] == "current copy only — no version history"
    assert v["as_of"].startswith("2026-09-06")


def test_archive_mirror_note_mentions_deletion(dirs):
    cache = dirs["cache"]
    _write_points(cache, "movies", {"kind": "current-copy", "folders": [], "as_of": "2026-09-06T06:09:00Z"})
    v = points.view(cache, {"name": "movies", "type": "archive", "storage_class": "STANDARD", "mirror": True})
    assert "deleted at home" in v["note"]


def test_archive_size_unknown_without_usage(dirs):
    cache = dirs["cache"]
    _write_points(cache, "movies", {"kind": "current-copy", "folders": [], "as_of": "2026-09-06T06:09:00Z"})
    v = points.view(cache, {"name": "movies", "type": "archive", "storage_class": "STANDARD"})
    assert v["size_bytes"] is None
    assert v["size_provenance"] == "assumed"


# --- versioned-files (File history) ---------------------------------------

def test_versioned_files_from_catalog_and_runs(dirs):
    cache = dirs["cache"]
    conn = catalog.open_catalog(Path(cache, "photos.sqlite"))
    catalog.record_version(conn, "a.jpg", "media/photos/a.jpg@1-x", 100, 1.0, "STANDARD", "2026-09-15T05:00:00Z")
    catalog.record_version(conn, "b.jpg", "media/photos/b.jpg@1-y", 250, 1.0, "STANDARD", "2026-09-15T05:00:00Z")
    conn.commit(); conn.close()
    _seed_ok_backup(cache, "photos", "20260915T050001Z-91aa", "2026-09-15T05:03:00Z",
                    type_="versioned-files", files_added=12, bytes_added=40_120_033)
    _seed_ok_backup(cache, "photos", "20260914T050001Z-77bb", "2026-09-14T05:03:00Z",
                    type_="versioned-files", files_added=3, bytes_added=1000)
    v = points.view(cache, {"name": "photos", "type": "versioned-files", "storage_class": "STANDARD"})
    assert v["kind"] == "file-history"
    assert v["count"] == 2
    assert v["file_count"] == 2
    assert v["size_bytes"] == 350
    assert v["size_source"] == "catalog"
    # newest first, one row per OK run, id = run id
    assert v["points"][0]["id"] == "20260915T050001Z-91aa"
    assert v["points"][0]["files_added"] == 12
    assert v["points"][0]["bytes_added"] == 40_120_033


# --- empty + stale ---------------------------------------------------------

def test_empty_cache_shape(dirs):
    cache = dirs["cache"]
    v = points.view(cache, {"name": "appdata", "type": "versioned", "storage_class": "STANDARD"})
    assert v["points"] == []
    assert v["count"] == 0
    assert v["fetched_at"] is None
    assert v["stale"] is True


def test_stale_when_cache_older_than_last_ok_run(dirs):
    cache = dirs["cache"]
    p = _write_points(cache, "appdata", [_snap("a", "2026-09-15T05:00:01Z")])
    # cache written "in the past"
    old = datetime(2026, 9, 15, 5, 0, 30, tzinfo=timezone.utc).timestamp()
    os.utime(p, (old, old))
    _seed_ok_backup(cache, "appdata", "20260916T050001Z-aa11", "2026-09-16T05:00:10Z")
    v = points.view(cache, {"name": "appdata", "type": "versioned", "storage_class": "STANDARD"})
    assert v["stale"] is True


def test_not_stale_when_cache_newer_than_last_ok_run(dirs):
    cache = dirs["cache"]
    _seed_ok_backup(cache, "appdata", "20260915T050001Z-aa11", "2026-09-15T05:00:10Z")
    p = _write_points(cache, "appdata", [_snap("a", "2026-09-15T05:00:01Z")])
    new = datetime(2026, 9, 15, 5, 1, 0, tzinfo=timezone.utc).timestamp()
    os.utime(p, (new, new))
    v = points.view(cache, {"name": "appdata", "type": "versioned", "storage_class": "STANDARD"})
    assert v["stale"] is False
