# Data Explorer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Browse a job's backed-up S3 contents from the GUI across all three engine types and restore/download any selected node to the restore mount.

**Architecture:** No change to storage or backup. Add (1) read-only per-type "browse" listing (a pure Python tree-folder + a read-only catalog query + new `restore.sh browse` subcommands), (2) a top-level Explore GUI surface (read-only GET routes + a job list + a per-job tree browser), and (3) a CSRF-guarded action route that reuses the existing restore pipeline (`ops.launch`/`_restore_argv`) to fetch a selected path to RESTORE_ROOT, routing cold nodes through the existing thaw flow.

**Tech Stack:** Python 3 (Flask GUI, `app/engine/*`), bash runner (restic/rclone/aws-cli), SQLite (vfiles catalog), Jinja2, vanilla JS, pytest + bats.

**Spec:** `docs/superpowers/specs/2026-09-22-data-explorer-design.md`

## Global Constraints

- **Read + recover only** — the explorer never edits, deletes, moves, or re-uploads stored data; no new storage format; no change to how backups are written.
- **Estimator FROZEN** — do not touch `app/estimator/*` (hash-guard `tests/estimator/test_untouched.py`).
- **Listing endpoints are read-only** — GET, no CSRF, no lock, no run record, never mutate; a listing failure degrades to an inline error, never a 500.
- **Actions are CSRF-only** (no typed-name confirm): `security.verify_csrf`; target confined to RESTORE_ROOT via `ops.validate_target`; single-flight via `ops.ensure_free`.
- **Path confinement** — a browse/action path is a job-relative POSIX path; reject absolute paths, `..` segments, and empty/`.` escapes (`PathError → 404`, never echo the bad path). Versioned browse always goes through `restic … --tag <job>`, never raw S3.
- **Cold data** (GLACIER/DEEP_ARCHIVE) — listing metadata always works; a fetch of a cold, un-thawed node routes through the existing thaw flow; never imply instant download.
- **Restic browse** — run `restic ls --json <snap>` at most once per snapshot; cache the full listing permanently at `state/<job>.browse/<snap>.json` (snapshots are immutable).
- **Entry JSON shape** (every listing level returns this): `{"path": "<cur_dir>", "entries": [{"name": str, "kind": "dir"|"file", "size": int|null, "storage_class": str|null, "modified": str|null, "versions": [ {"version_id","uploaded_at","storage_class","size","deleted"} ]  (files only, vfiles only) }]}`. `dir` entries always precede `file` entries; within each, sorted by name.
- **Reuse, don't fork** the restore pipeline: fill the existing (currently-unused) `path`/`scope` wiring in `_restore_argv`; call the same `ops.launch`/`ops.validate_target`/`ops.ensure_free`.
- Commit per task. Do NOT deploy. Branch: `data-explorer` (off master).

---

### Task 1: Pure tree-fold module (`app/engine/browse.py`)

**Files:**
- Create: `app/engine/browse.py`
- Test: `tests/engine/test_browse.py`

**Interfaces:**
- Produces:
  - `fold_level(records: list[dict], cur_dir: str) -> dict` — `records` each have at least `"path"` (a POSIX relpath) and optionally `"type"` (`"file"`/`"dir"`), `"size"`, `"storage_class"`, `"modified"`. Returns `{"dirs": [name:str ...], "files": [record + "name" ...]}`: `dirs` = the distinct next path-segment of every record strictly below `cur_dir`; `files` = records whose parent dir == `cur_dir` and whose `type` != `"dir"`. Both sorted by name.
  - `level_from_restic_ls(ls_json_path: str, cur_dir: str) -> dict` — reads a restic `ls --json` output file (JSON-Lines: a leading snapshot object then one object per node with `struct_type`/`type`, `path`, `size`), folds to the entry shape `{"path", "entries":[...]}`.
  - CLI `python3 -m app.engine.browse <ls_json_file> <cur_dir>` → prints the `level_from_restic_ls` JSON (used by `restore.sh` in Task 5).

- [ ] **Step 1: Write failing tests**
```python
# tests/engine/test_browse.py
import json
from app.engine import browse

def test_fold_level_root():
    recs = [{"path": "a/b.txt", "type": "file", "size": 3},
            {"path": "a/c.txt", "type": "file", "size": 4},
            {"path": "top.txt", "type": "file", "size": 1}]
    out = browse.fold_level(recs, "")
    assert out["dirs"] == ["a"]
    assert [f["name"] for f in out["files"]] == ["top.txt"]

def test_fold_level_subdir():
    recs = [{"path": "a/b.txt", "type": "file", "size": 3},
            {"path": "a/sub/d.txt", "type": "file", "size": 9}]
    out = browse.fold_level(recs, "a")
    assert out["dirs"] == ["sub"]
    assert [f["name"] for f in out["files"]] == ["b.txt"]

def test_level_from_restic_ls(tmp_path):
    p = tmp_path / "ls.json"
    p.write_text("\n".join([
        json.dumps({"time": "2026-09-01T00:00:00Z", "struct_type": "snapshot"}),
        json.dumps({"struct_type": "node", "type": "dir",  "path": "/etc"}),
        json.dumps({"struct_type": "node", "type": "file", "path": "/etc/hosts", "size": 12}),
        json.dumps({"struct_type": "node", "type": "file", "path": "/top", "size": 1}),
    ]))
    out = browse.level_from_restic_ls(str(p), "")
    # restic paths are absolute; the leading "/" is normalized away to a job-root-relative view
    assert out["path"] == ""
    kinds = {e["name"]: e["kind"] for e in out["entries"]}
    assert kinds == {"etc": "dir", "top": "file"}
    sub = browse.level_from_restic_ls(str(p), "etc")
    assert [e["name"] for e in sub["entries"]] == ["hosts"]
```

- [ ] **Step 2: Run → FAIL** (`python3 -m pytest tests/engine/test_browse.py -v`)

- [ ] **Step 3: Implement**
```python
# app/engine/browse.py
"""Pure tree-folding for the data explorer. No I/O except level_from_restic_ls
reading a cache file. Turns a flat list of path records into one directory level."""
from __future__ import annotations
import json, sys

def _norm(p: str) -> str:
    return p.strip("/")

def _parent(p: str) -> str:
    p = _norm(p)
    return p.rsplit("/", 1)[0] if "/" in p else ""

def _name(p: str) -> str:
    return _norm(p).rsplit("/", 1)[-1]

def fold_level(records: list[dict], cur_dir: str) -> dict:
    cur = _norm(cur_dir)
    prefix = (cur + "/") if cur else ""
    dirs: dict[str, bool] = {}
    files = []
    for r in records:
        path = _norm(r.get("path", ""))
        if not path or (cur and not path.startswith(prefix)):
            continue
        rest = path[len(prefix):]
        if "/" in rest:                       # something deeper -> a subdir at this level
            dirs.setdefault(rest.split("/", 1)[0], True)
        elif rest and r.get("type") != "dir":  # a file directly at this level
            files.append({**r, "name": rest})
    return {"dirs": sorted(dirs), "files": sorted(files, key=lambda f: f["name"])}

def _entries(level: dict, cur: str) -> dict:
    entries = [{"name": d, "kind": "dir", "size": None, "storage_class": None, "modified": None}
               for d in level["dirs"]]
    for f in level["files"]:
        entries.append({"name": f["name"], "kind": "file", "size": f.get("size"),
                        "storage_class": f.get("storage_class"), "modified": f.get("modified"),
                        **({"versions": f["versions"]} if "versions" in f else {})})
    return {"path": _norm(cur), "entries": entries}

def level_from_restic_ls(ls_json_path: str, cur_dir: str) -> dict:
    records = []
    with open(ls_json_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("struct_type") == "node" or ("path" in o and "message_type" not in o and o.get("struct_type") != "snapshot"):
                if "path" in o:
                    records.append({"path": o["path"], "type": o.get("type"), "size": o.get("size")})
    return _entries(fold_level(records, cur_dir), cur_dir)

if __name__ == "__main__":                      # python3 -m app.engine.browse <ls_json> <cur_dir>
    print(json.dumps(level_from_restic_ls(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")))
```

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/engine/browse.py tests/engine/test_browse.py && git commit -m "feat(explore): pure tree-fold + restic-ls level folding"`

---

### Task 2: Read-only catalog browse (`app/engine/catalog.py`)

**Files:**
- Modify: `app/engine/catalog.py` (add `browse`; reuse existing `versions`)
- Test: `tests/engine/test_catalog.py` (add cases; create if absent)

**Interfaces:**
- Consumes: `browse.fold_level` (Task 1); the existing `catalog.versions(conn, path)` (newest-first version rows) and the `versions` table (`path, size, storage_class, uploaded_at, is_current, deleted`).
- Produces: `catalog.browse(conn, cur_dir: str) -> dict` = the entry shape `{"path", "entries":[...]}`. `dir` entries first; each `file` entry carries `size`/`storage_class`/`modified` (from its current version) plus a `versions` list `[{version_id, uploaded_at, storage_class, size, deleted}]` (newest-first) from `catalog.versions`. Read-only; never writes.

- [ ] **Step 1: Write failing test**
```python
# tests/engine/test_catalog.py  (add)
from app.engine import catalog

def _seed(conn):
    catalog.init(conn)  # or the existing schema-init helper the module exposes
    # two versions of a/b.txt, one top.txt, one deeper a/sub/d.txt
    conn.executescript("""
      INSERT INTO versions(path,key,size,mtime,storage_class,uploaded_at,is_current,deleted) VALUES
        ('a/b.txt','media/j/a/b.txt@1-aa',3,0,'STANDARD','2026-09-01T00:00:00Z',0,0),
        ('a/b.txt','media/j/a/b.txt@2-bb',5,0,'STANDARD','2026-09-02T00:00:00Z',1,0),
        ('top.txt','media/j/top.txt@1-cc',1,0,'DEEP_ARCHIVE','2026-09-01T00:00:00Z',1,0),
        ('a/sub/d.txt','media/j/a/sub/d.txt@1-dd',9,0,'STANDARD','2026-09-01T00:00:00Z',1,0);
    """)
    conn.commit()

def test_catalog_browse_root(tmp_path):
    import sqlite3
    conn = sqlite3.connect(":memory:"); _seed(conn)
    out = catalog.browse(conn, "")
    assert [e["name"] for e in out["entries"] if e["kind"] == "dir"] == ["a"]
    top = next(e for e in out["entries"] if e["name"] == "top.txt")
    assert top["kind"] == "file" and top["storage_class"] == "DEEP_ARCHIVE"

def test_catalog_browse_subdir_has_version_history(tmp_path):
    import sqlite3
    conn = sqlite3.connect(":memory:"); _seed(conn)
    out = catalog.browse(conn, "a")
    b = next(e for e in out["entries"] if e["name"] == "b.txt")
    assert b["size"] == 5  # current version
    assert [v["uploaded_at"] for v in b["versions"]] == ["2026-09-02T00:00:00Z", "2026-09-01T00:00:00Z"]
```
(Use whatever schema-init the module already exposes — grep `_SCHEMA`/`init`; the seed SQL matches the real `versions` columns.)

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
```python
# app/engine/catalog.py  (add near the other read helpers)
from . import browse as _browse

def browse(conn, cur_dir: str) -> dict:
    """Read-only: fold current (is_current=1, deleted=0) paths into one directory
    level; attach per-file version history. Never writes."""
    rows = conn.execute(
        "SELECT path, size, storage_class, uploaded_at FROM versions "
        "WHERE is_current=1 AND deleted=0").fetchall()
    records = [{"path": r[0], "type": "file", "size": r[1],
                "storage_class": r[2], "modified": r[3]} for r in rows]
    level = _browse.fold_level(records, cur_dir)
    for f in level["files"]:
        f["versions"] = [
            {"version_id": v.key if hasattr(v, "key") else v["key"],
             "uploaded_at": v.uploaded_at if hasattr(v, "uploaded_at") else v["uploaded_at"],
             "storage_class": v.storage_class if hasattr(v, "storage_class") else v["storage_class"],
             "size": v.size if hasattr(v, "size") else v["size"],
             "deleted": bool(v.deleted if hasattr(v, "deleted") else v["deleted"])}
            for v in versions(conn, f["path"])]
    return _browse._entries(level, cur_dir)
```
(Adjust the `versions()` row access to the real return type — if `versions` returns sqlite rows/tuples, index them; if dataclasses, use attributes. Match the existing `catalog.versions` contract exactly.)

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/engine/catalog.py tests/engine/test_catalog.py && git commit -m "feat(explore): read-only catalog.browse with per-file version history"`

---

### Task 3: vfiles `browse` CLI (`app/engine/vfiles.py`)

**Files:**
- Modify: `app/engine/vfiles.py` (add a `browse` subcommand beside the existing `restore … list`)
- Test: `tests/engine/test_vfiles.py` (add)

**Interfaces:**
- Consumes: the existing `_open_or_fetch_catalog(job)` (opens the local sqlite, fetching from S3 if needed) and `catalog.browse` (Task 2).
- Produces: `python3 -m app.engine.vfiles browse <job> [<relpath>] [--json]` → prints `catalog.browse` JSON for the level. Read-only (open the catalog read-only; never write it back).

- [ ] **Step 1: Write failing test**
```python
# tests/engine/test_vfiles.py  (add) — drive the module entry with a seeded local catalog
def test_vfiles_browse_json(tmp_path, monkeypatch, capsys):
    import sqlite3, json
    from app.engine import vfiles, catalog
    cat = tmp_path / "j.sqlite"
    conn = sqlite3.connect(str(cat)); catalog.init(conn)
    conn.execute("INSERT INTO versions(path,key,size,mtime,storage_class,uploaded_at,is_current,deleted)"
                 " VALUES('docs/a.txt','k',2,0,'STANDARD','2026-09-01T00:00:00Z',1,0)")
    conn.commit(); conn.close()
    monkeypatch.setattr(vfiles, "_open_or_fetch_catalog", lambda job: sqlite3.connect(f"file:{cat}?mode=ro", uri=True))
    vfiles.main(["browse", "j", "docs", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "docs" and out["entries"][0]["name"] == "a.txt"
```
(Match the real module entry — if it uses `argparse` with subcommands and a `main(argv)`; grep the existing `restore`/`list` dispatch and mirror it.)

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — add a `browse` subcommand to the module's arg dispatch:
```python
# app/engine/vfiles.py  (in the CLI dispatch, beside 'restore')
def _cmd_browse(job, relpath="", as_json=False):
    from . import catalog
    conn = _open_or_fetch_catalog(job)
    try:
        level = catalog.browse(conn, relpath or "")
    finally:
        try: conn.close()
        except Exception: pass
    if as_json:
        import json; print(json.dumps(level))
    else:
        for e in level["entries"]:
            print(f"{e['kind']}\t{e['name']}\t{e.get('storage_class') or ''}")
```
Wire it into the existing subcommand parser: `browse <job> [relpath] [--json]`.

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/engine/vfiles.py tests/engine/test_vfiles.py && git commit -m "feat(explore): vfiles browse CLI over the catalog"`

---

### Task 4: `restore.sh browse` — archive + vfiles

**Files:**
- Modify: `scripts/restore.sh` (add a `browse` subcommand to `_dispatch`, archive + vfiles arms; a `_safe_rel` guard)
- Test: `tests/bats/restore.bats` (add; create if the file doesn't exist — mirror `backup-job.bats` harness)

**Interfaces:**
- Consumes: existing `_restore_archive`/`_restore_vfiles` dispatch by `$JOB_TYPE`; the archive bucket var `$BUCKET` and `media/$JOB/` layout the existing archive `list` uses; `python3 -m app.engine.vfiles browse` (Task 3).
- Produces: `restore.sh <job> browse [<relpath>] [--json]` (read-only, no lock, no run record — same contract as `list`). archive → `rclone lsjson` mapped to the entry JSON; vfiles → delegate to the vfiles CLI. Rejects unsafe paths.

- [ ] **Step 1: Write failing bats tests** (stub `rclone`)
```bash
# tests/bats/restore.bats
load test_helper
@test "archive browse lists one level as entry JSON" {
  cat >"$BATS_TEST_TMPDIR/bin/rclone" <<'EOF'
#!/usr/bin/env bash
# emulate: rclone lsjson s3:bucket/media/cfg/<path>
echo '[{"Name":"sub","IsDir":true},{"Name":"a.txt","IsDir":false,"Size":12,"Tier":"STANDARD","ModTime":"2026-09-01T00:00:00Z"}]'
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/rclone"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=archive; echo JOB_SOURCE=media; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  run run_restore cfg browse "" --json
  [ "$status" -eq 0 ]
  echo "$output" | grep -q '"kind":"dir"'
  echo "$output" | grep -q '"name":"a.txt"'
}
@test "browse rejects a path escape" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=archive; echo JOB_SOURCE=media\n' >"$JOBS_IO_STUB"
  run run_restore cfg browse "../secret" --json
  [ "$status" -ne 0 ]
}
```
(Follow the existing bats harness — `run_restore`/`JOBS_IO_STUB` mirroring how `backup-job.bats` stubs `jobs_io` and puts stubs on PATH.)

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — in `restore.sh`:
```bash
# path guard: reject absolute, empty-after-normalize, or any '..' segment
_safe_rel() {  # prints the cleaned relpath or exits non-zero
  local p="${1#/}"
  case "$p" in *..*) return 1;; esac
  printf '%s' "$p"
}
# archive arm of `browse` (inside _restore_archive, new subcommand):
#   rclone lsjson "s3:$BUCKET/media/$JOB/$rel"  -> map to entry JSON via a tiny python filter
_browse_archive() {
  local rel; rel="$(_safe_rel "${1:-}")" || _die "bad path"
  rclone lsjson "s3:$BUCKET/media/$JOB/${rel:+$rel/}" 2>/dev/null | python3 -c '
import sys, json
items = json.load(sys.stdin) if sys.stdin.read.__self__ else []
' 2>/dev/null || true
}
```
> Implementation note: keep the rclone→entry mapping in a small Python one-liner/module so bash stays thin. Map each rclone item `{Name,IsDir,Size,Tier,ModTime}` → `{"name":Name,"kind":"dir" if IsDir else "file","size":Size,"storage_class":Tier,"modified":ModTime}`, sort dirs-first then by name, and wrap as `{"path":rel,"entries":[...]}`. Add a helper `app/engine/browse.py:level_from_rclone_lsjson(json_text, cur_dir)` (pure) and call `python3 -m app.engine.browse --rclone <cur_dir>` reading stdin — extend Task 1's CLI with an `--rclone` mode rather than inlining Python in bash. The vfiles arm simply calls `python3 -m app.engine.vfiles browse "$JOB" "$rel" --json`. Route `browse` in `_dispatch` to these read-only arms (no `acquire_lock`, no run record), exactly like `list`/`thaw-status` (restore.sh:main read-only branch).

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add scripts/restore.sh app/engine/browse.py tests/bats/restore.bats && git commit -m "feat(explore): restore.sh browse for archive + vfiles"`

---

### Task 5: `restore.sh browse` — versioned (restic) + cache + delete cleanup

**Files:**
- Modify: `scripts/restore.sh` (versioned arm of `browse`)
- Modify: `app/gui/jobs_io.py` (`_remove_job_caches` also removes `state/<job>.browse/`)
- Test: `tests/bats/restore.bats` (add), `tests/gui/test_jobs_io.py` (add)

**Interfaces:**
- Consumes: existing restic env (`RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE`, `RESTIC_CACHE_DIR`) and `require_env`; `python3 -m app.engine.browse <cache_file> <cur_dir>` (Task 1).
- Produces: `restore.sh <job> browse --snapshot <snap> [<relpath>] [--json]` → ensure `$CACHE_DIR/state/$JOB.browse/<snap>.json` exists (run `restic ls --json <snap> --tag <job>` once if not), then fold+print the level via the browse CLI. `jobs_io.delete` removes `state/<job>.browse/`.

- [ ] **Step 1: Write failing bats + pytest**
```bash
# tests/bats/restore.bats  (add) — restic ls runs ONCE, second browse hits cache
@test "versioned browse caches restic ls per snapshot" {
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
if [[ "$*" == *"ls"* ]]; then
  printf '%s\n' '{"struct_type":"snapshot"}' '{"struct_type":"node","type":"file","path":"/a.txt","size":3}'
fi
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata\n' >"$JOBS_IO_STUB"
  run run_restore cfg browse --snapshot deadbeef "" --json
  [ "$status" -eq 0 ]; echo "$output" | grep -q '"name":"a.txt"'
  run run_restore cfg browse --snapshot deadbeef "" --json
  [ "$(grep -c ' ls ' "$RESTIC_LOG")" -eq 1 ]   # cached: restic ls ran only once
}
```
```python
# tests/gui/test_jobs_io.py  (add)
def test_delete_removes_browse_cache_dir(tmp_path):
    from app.gui import jobs_io
    from pathlib import Path
    cfg = tmp_path/"config"; cfg.mkdir(); (cfg/"jobs.json").write_text('{"jobs":[{"name":"j","type":"versioned","source":"appdata","schedule":"0 5 * * *","enabled":true,"storage_class":"STANDARD","keep":{"last":1}}]}')
    cache = tmp_path/"cache"; d = Path(cache,"state","j.browse"); d.mkdir(parents=True); (d/"snap.json").write_text("[]")
    jobs_io.delete(str(cfg), "j", cache_dir=str(cache))
    assert not d.exists()
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
```bash
# scripts/restore.sh — versioned arm of browse
_browse_versioned() {  # args: <snap> <rel>
  local snap="$1" rel; rel="$(_safe_rel "${2:-}")" || _die "bad path"
  [ -n "$snap" ] || _die "snapshot required"
  local dir="$CACHE_DIR/state/$JOB.browse"; mkdir -p "$dir"
  local cache="$dir/$snap.json"
  if [ ! -s "$cache" ]; then
    RESTIC_CACHE_DIR="$CACHE_DIR/restic" restic -r "$RESTIC_REPOSITORY" ls --json "$snap" --tag "$JOB" \
      >"$cache.tmp" 2>/dev/null && mv -f "$cache.tmp" "$cache" || { rm -f "$cache.tmp"; _die "restic ls failed"; }
  fi
  python3 -m app.engine.browse "$cache" "$rel"
}
```
```python
# app/gui/jobs_io.py — in _remove_job_caches, after the logs/runs rmtree:
    shutil.rmtree(Path(cache_dir, "state", f"{name}.browse"), ignore_errors=True)
```
Parse `--snapshot <snap>` in the versioned `browse` dispatch; snapshot ids come from the existing `restic snapshots --tag <job> --json` list (already cached at `state/<job>.points.json`).

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add scripts/restore.sh app/gui/jobs_io.py tests/bats/restore.bats tests/gui/test_jobs_io.py && git commit -m "feat(explore): restic browse with per-snapshot cache + delete cleanup"`

---

### Task 6: Explore read-only routes (`/explore`, `/explore/<job>`, list.json)

**Files:**
- Modify: `app/gui/routes.py` (add `explore_index`, `explore_job`, `explore_list`)
- Test: `tests/gui/test_explore_routes.py` (create)

**Interfaces:**
- Consumes: `jobs_io.load`/`jobs_io.get`; the runner via a helper that shells `restore.sh <job> browse …` read-only (mirror how `points.refresh` shells `restore.sh`), OR call `catalog.browse` directly for vfiles; `security.issue_csrf`.
- Produces: `GET /explore` (job list) → `explore_index.html`; `GET /explore/<job>` (per-job browser) → `explore.html`; `GET /explore/<job>/list.json` → the entry JSON for a level (params `path`, `snapshot`). Unknown job → themed 404. Read-only; never mutate; a tool failure → `{"error": "..."}` (200) or an inline error, never a 500.

- [ ] **Step 1: Write failing tests**
```python
# tests/gui/test_explore_routes.py
def test_explore_index_lists_jobs(client, example):
    body = client.get("/explore").get_data(as_text=True)
    assert client.get("/explore").status_code == 200 and "appdata" in body

def test_explore_job_renders(client, example):
    assert client.get("/explore/appdata").status_code == 200

def test_explore_unknown_job_404(client, example):
    assert client.get("/explore/nope").status_code == 404

def test_explore_list_json_archive(client, example, monkeypatch):
    import app.gui.routes as routes
    monkeypatch.setattr(routes, "_browse_level",
        lambda cfg, name, jt, path, snapshot=None: {"path": path, "entries": [{"name":"a.txt","kind":"file","size":1,"storage_class":"STANDARD","modified":None}]})
    out = client.get("/explore/manga/list.json?path=").get_json()
    assert out["entries"][0]["name"] == "a.txt"
```
(Reuse the `client`/`example` fixtures from `tests/gui/test_job_page_routes.py` — copy the fixture setup or import the shared conftest; `example` seeds an `appdata` versioned + `manga` archive job.)

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — add a `_browse_level(cfg, name, job_type, path, snapshot=None) -> dict` helper that: for `archive`/`versioned-files` shells `restore.sh <job> browse <path> --json` (versioned-files) / archive respectively (capture stdout, `json.loads`); for `versioned` shells `restore.sh <job> browse --snapshot <snap> <path> --json`; returns `{"error": msg}` on any failure (never raises). Then:
```python
# app/gui/routes.py
@bp.get("/explore")
def explore_index():
    cfg = current_app.config
    jobs = jobs_io.load(cfg["CONFIG_DIR"])
    return render_template("explore_index.html", jobs=jobs, csrf=security.issue_csrf())

@bp.get("/explore/<name>")
def explore_job(name):
    cfg = current_app.config
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    return render_template("explore.html", job=job, name=name, csrf=security.issue_csrf())

@bp.get("/explore/<name>/list.json")
def explore_list(name):
    cfg = current_app.config
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    path = request.args.get("path", "")
    snap = request.args.get("snapshot")
    return jsonify(_browse_level(cfg, name, job.get("type"), path, snapshot=snap))
```

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/gui/routes.py tests/gui/test_explore_routes.py && git commit -m "feat(explore): read-only Explore routes + list.json"`

---

### Task 7: Explore action route (`POST /explore/<job>/get`)

**Files:**
- Modify: `app/gui/routes.py` (add `explore_get`)
- Test: `tests/gui/test_explore_routes.py` (add)

**Interfaces:**
- Consumes: `security.verify_csrf`; `ops.validate_target`/`ops.ensure_free`/`ops.launch`; the existing `_restore_argv` (fill its `path`/`scope` for a targeted fetch); `points.COLD_CLASSES`; the existing thaw route/helper for cold nodes.
- Produces: `POST /explore/<name>/get` — CSRF only; validates+confines `path` (`_safe_rel`→404) and target (→RESTORE_ROOT); 409 on busy lock; launches the per-type targeted restore (`restore.sh <job> restore <snap> <target> --include <path>` | `download <path> <target>` | `<path> <target> [--asof]`) via `ops.launch` and redirects to the run page. A cold, un-thawed node routes to the existing thaw action instead.

- [ ] **Step 1: Write failing tests**
```python
def test_explore_get_requires_csrf(client, example):
    assert client.post("/explore/manga/get", data={"path": "a.txt", "target": "x"}).status_code == 400

def test_explore_get_rejects_path_escape(client, example):
    t = _csrf(client, "/explore/manga")
    assert client.post("/explore/manga/get", data={"csrf": t, "path": "../x", "target": "y"}).status_code == 404

def test_explore_get_launches_targeted_restore(client, example, monkeypatch):
    import app.gui.routes as routes
    launched = {}
    monkeypatch.setattr(routes.ops, "ensure_free", lambda *a, **k: None)
    monkeypatch.setattr(routes.ops, "validate_target", lambda *a, **k: a[-1] if a else "t")
    monkeypatch.setattr(routes.ops, "launch", lambda cfg, argv, **k: launched.setdefault("argv", argv) or "RUNID")
    t = _csrf(client, "/explore/manga")
    r = client.post("/explore/manga/get", data={"csrf": t, "path": "docs/a.txt", "target": "out"})
    assert r.status_code in (302, 303)
    assert "docs/a.txt" in " ".join(launched["argv"])
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
```python
# app/gui/routes.py
@bp.post("/explore/<name>/get")
def explore_get(name):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    f = request.form
    try:
        rel = _safe_rel_py(f.get("path", ""))         # mirror bash _safe_rel; PathError -> 404
    except ValueError:
        abort(404, description="bad path")
    storage_class = f.get("storage_class", "")
    if storage_class in points.COLD_CLASSES and f.get("thawed") != "1":
        return _thaw_for(cfg, name, job, rel)          # reuse the existing thaw pipeline
    target = ops.validate_target(cfg, f.get("target", ""))
    ops.ensure_free(cfg, name)                          # 409 if busy
    argv = _restore_argv(job, name, intent="restore", path=rel,
                         snapshot=f.get("snapshot"), asof=f.get("asof"), target=target)
    run_id = ops.launch(cfg, argv, job=name, kind="restore")
    return redirect(url_for("gui.run_page", name=name, run_id=run_id))
```
Add `_safe_rel_py(path)` (reject absolute/`..`/empty → `ValueError`) and, if `_restore_argv` doesn't already accept `path`/`snapshot`/`asof`/`target` kwargs, thread them (the recon confirms the `path`/`scope=file` wiring already exists for vfiles — extend to all three types). `_thaw_for` wraps the existing thaw launch for the selected scope.

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/gui/routes.py tests/gui/test_explore_routes.py && git commit -m "feat(explore): CSRF action route -> targeted restore/thaw"`

---

### Task 8: Explore templates + nav + job-page link

**Files:**
- Create: `app/gui/templates/explore_index.html`, `app/gui/templates/explore.html`
- Modify: `app/gui/templates/base.html` (nav item), `app/gui/templates/job.html` (a "Browse contents →" link)
- Test: `tests/gui/test_explore_routes.py` (add render assertions)

**Interfaces:**
- Consumes: `explore_index` context (`jobs`), `explore_job` context (`job`, `name`, `csrf`); the `list.json` endpoint (Task 6) for the initial level (server-rendered) and lazy loads (Task 9).

- [ ] **Step 1: Write failing test**
```python
def test_explore_index_has_links_and_nav(client, example):
    body = client.get("/explore").get_data(as_text=True)
    assert '/explore/appdata' in body
    assert 'href="/explore"' in body  # nav item present on the page
def test_explore_job_has_get_form_with_csrf(client, example):
    body = client.get("/explore/manga").get_data(as_text=True)
    assert '/explore/manga/get' in body and 'name="csrf"' in body
def test_job_page_links_to_explore(client, example):
    assert '/explore/appdata' in client.get("/jobs/appdata").get_data(as_text=True)
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
  - `explore_index.html` (`{% extends "base.html" %}`, `<section class="screen dense">`): one row per job — `<a href="/explore/{{ j.name }}">{{ j.name }}</a>`, plain-type label, size if available.
  - `explore.html` (`<section class="screen dense">`): a breadcrumb from the current `?path=`; a listing table (dirs first, then files: name, size, storage-class token, modified); for a `versioned` job a snapshot `<select>` (options from the points cache) that reloads with `?snapshot=`; for `versioned-files` a per-file version affordance; a **cold** badge on cold nodes. Each row carries a CSRF `<form method="post" action="/explore/{{ name }}/get">` with hidden `path`/`snapshot`/`storage_class` and a `target` input (default under RESTORE_ROOT). The page works with plain full-page `?path=` navigation (no-JS fallback) — Task 9 adds lazy loading.
  - `base.html`: add `<a href="/explore" ...>Explore</a>` to the nav, following the existing nav-item markup (Board · Cost · Activity · Setup).
  - `job.html`: add a `Browse contents →` link to `/explore/{{ s.name }}` near the restore band.

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/gui/templates/explore_index.html app/gui/templates/explore.html app/gui/templates/base.html app/gui/templates/job.html tests/gui/test_explore_routes.py && git commit -m "feat(explore): Explore screens + nav + job-page link"`

---

### Task 9: Lazy level loading (`app/gui/static/app.js`)

**Files:**
- Modify: `app/gui/static/app.js` (add a guarded Explore-browser enhancement)
- Test: none (JS; keep it a minimal, obviously-safe progressive enhancement — the no-JS `?path=` navigation from Task 8 remains the correctness baseline)

**Interfaces:**
- Consumes: `GET /explore/<job>/list.json?path=&snapshot=` (Task 6).

- [ ] **Step 1: Implement** — a self-contained IIFE (mirroring the existing progress IIFE's defensive style): if an element `[data-explore-job]` exists, intercept clicks on directory links, `fetch` `list.json` for the new path, and repaint the listing + breadcrumb without a full navigation; fall back to normal navigation on any error (never throw). Handle the `{"building": true}` response (restic cache warming) with a "loading contents…" state + retry, and `{"error": …}` with an inline message.
- [ ] **Step 2: Manually verify** the page still browses with JS disabled (full-page `?path=`) and with JS enabled (in-place). Run the full suite to confirm no regressions: `python3 -m pytest -q` and `bats tests/bats/`.
- [ ] **Step 3: Commit** `git add app/gui/static/app.js && git commit -m "feat(explore): lazy in-place level loading (progressive enhancement)"`

---

## Self-Review

**Spec coverage:** browse per type (archive/vfiles/restic) → T1–T5; on-demand+cache for restic → T1+T5; top-level Explore nav + job list + per-job browser → T6+T8; list.json lazy endpoint → T6+T9; targeted restore/download reusing the pipeline, CSRF-only → T7; cold→thaw routing → T7; path/target confinement → T4/T5/T7; delete cleanup of the browse cache → T5; non-goals (no preview/stream/browser-download/search/edit) → not implemented by any task (correct). Estimator untouched. No spec requirement is unmapped.

**Placeholder scan:** every code step carries real code or a precise integration note pointing at named existing functions (`_restore_argv`, `ops.launch`, `_open_or_fetch_catalog`, `points.COLD_CLASSES`); the bash `browse` archive mapping is delegated to a pure Python helper (`browse.level_from_rclone_lsjson`) rather than inline bash JSON. The one place the exact row-access type is unknown (`catalog.versions` return shape) is called out explicitly to match the real contract.

**Type consistency:** the entry JSON shape (`{"path","entries":[{name,kind,size,storage_class,modified,versions?}]}`) is defined in Global Constraints and produced identically by `browse._entries` (T1), `catalog.browse` (T2), the archive rclone mapping (T4), and consumed by `list.json` (T6), the templates (T8), and app.js (T9). `_browse_level(cfg,name,job_type,path,snapshot=None)` is defined in T6 and monkeypatched by the same signature in tests. `_safe_rel` (bash, T4) and `_safe_rel_py` (Python, T7) enforce the same rule on both sides. `restore.sh <job> browse [--snapshot <snap>] [<rel>] [--json]` is consistent across T4/T5/T6.
