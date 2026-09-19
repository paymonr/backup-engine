# tests/engine/test_progress.py — live progress reader (app/engine/progress.py).
# The restic status line shape is taken verbatim from a real run's <job>-last.jsonl.
import dataclasses
from pathlib import Path

from app.engine import progress, runs

# A real restic --json status line (mid-run and final), and the summary it ignores.
STATUS_MID = ('{"message_type":"status","seconds_elapsed":100,"percent_done":0.5,'
              '"total_files":533,"files_done":260,"total_bytes":10000000000,"bytes_done":5000000000}')
STATUS_DONE = ('{"message_type":"status","seconds_elapsed":281,"percent_done":1,'
               '"total_files":533,"files_done":533,"total_bytes":56596431691,"bytes_done":56596431691}')
SUMMARY = '{"message_type":"summary","files_new":533,"data_added":46135862832,"total_bytes_processed":56596431691}'


# --- restic parser ---------------------------------------------------------

def test_restic_midrun_line_normalizes():
    p = progress.parse_restic_status(STATUS_MID)
    assert p["engine"] == "restic"
    assert p["percent"] == 50.0
    assert p["bytes_done"] == 5_000_000_000 and p["bytes_total"] == 10_000_000_000
    assert p["files_done"] == 260 and p["files_total"] == 533
    assert p["seconds_elapsed"] == 100
    assert p["eta_seconds"] == 100          # 5 GB done in 100 s -> 5 GB left ~= 100 s


def test_restic_uses_last_status_and_ignores_summary():
    text = "\n".join([STATUS_MID, STATUS_DONE, SUMMARY])
    p = progress.parse_restic_status(text)
    assert p["percent"] == 100.0            # the LAST status wins
    assert p["eta_seconds"] == 0            # 100% -> done


def test_restic_no_status_returns_none():
    assert progress.parse_restic_status(SUMMARY) is None
    assert progress.parse_restic_status("") is None
    assert progress.parse_restic_status("not json\n{}") is None


# --- rclone parser (best-effort) -------------------------------------------

RCLONE_BLOCK = (
    "Transferred:   \t  1.500 GiB / 6.000 GiB, 25%, 4.000 MiB/s, ETA 6m1s\n"
    "Errors:                 0\n"
    "Transferred:            3 / 50, 6%\n"
    "Elapsed time:        1m2.3s\n")


def test_rclone_stats_block_parses():
    p = progress.parse_rclone_stats(RCLONE_BLOCK)
    assert p["engine"] == "rclone"
    assert p["percent"] == 25.0
    assert p["bytes_done"] == int(1.5 * 1024 ** 3)
    assert p["bytes_total"] == 6 * 1024 ** 3
    assert p["eta_seconds"] == 361          # 6m1s


def test_rclone_takes_the_last_block():
    two = RCLONE_BLOCK + "Transferred:   \t  3.000 GiB / 6.000 GiB, 50%, 4.000 MiB/s, ETA 3m0s\n"
    p = progress.parse_rclone_stats(two)
    assert p["percent"] == 50.0 and p["eta_seconds"] == 180


def test_rclone_no_match_returns_none():
    assert progress.parse_rclone_stats("nothing here") is None


# --- read_progress (integration) -------------------------------------------

def _fake_running(job="appdata_backup", kind="backup"):
    # Minimal RunRecord standing in for an active run.
    return runs.RunRecord(id="20260919T155129Z-9f96", job=job, kind=kind, trigger="manual",
                          outcome="running", started_at=None, finished_at=None, duration_s=None,
                          exit_code=None, error=None)


def _write_state(cache, name, text):
    p = Path(cache, "state", name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_read_progress_idle_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "active_run", lambda c, j: None)
    assert progress.read_progress(str(tmp_path), "appdata_backup", "versioned") == {"running": False}


def test_read_progress_restic_running(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "active_run", lambda c, j: _fake_running())
    _write_state(tmp_path, "appdata_backup-last.jsonl", "\n".join([STATUS_MID, STATUS_DONE]))
    out = progress.read_progress(str(tmp_path), "appdata_backup", "versioned")
    assert out["running"] is True and out["engine"] == "restic"
    assert out["percent"] == 100.0 and out["kind"] == "backup"


def test_read_progress_running_but_no_file_is_indeterminate(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "active_run", lambda c, j: _fake_running())
    out = progress.read_progress(str(tmp_path), "appdata_backup", "versioned")
    assert out["running"] is True and out["engine"] == "restic" and out["percent"] is None


def test_read_progress_vfiles_is_indeterminate(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "active_run", lambda c, j: _fake_running(kind="backup"))
    out = progress.read_progress(str(tmp_path), "docs", "versioned-files")
    assert out["running"] is True and out["engine"] == "vfiles" and out["percent"] is None
