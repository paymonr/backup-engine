# Data Explorer — design

Status: draft for review · 2026-09-22 · owner: paymon

## Summary

A read-and-recover **data explorer**: browse everything a job has backed up to S3,
directly from the GUI, across all three engine types, and restore or download any
selected node to the restore mount. Today the GUI only lets you pick a point-in-time
(restic/vfiles) or a top-level folder (archive) — never an individual file/path. This
adds a top-level **Explore** surface that walks the actual contents one directory level
at a time, adapts to each engine's storage model, and reuses the existing restore
pipeline for the "get it back" action.

**Reassuring baseline (already true):** archive jobs store a plain 1:1 S3 mirror at
`media/<job>/…`; versioned-files keep a per-job SQLite catalog (local + in S3) with full
per-path version history; restic keeps immutable, tagged snapshots in a shared
`appdata/` repo. So "browse" is, mechanically, "list what's already there" — no new
storage format, no change to how backups are written.

## Goals / Non-goals

**Goals**
- Browse a job's backed-up contents as a navigable folder tree, one level at a time.
- Cover all three engine types, adapting the browse model to each.
- From any node, launch a **targeted** restore/download of just that path (not the whole job) to the restore mount, reusing the existing guarded restore pipeline.
- Show storage class + cold-warm-up state honestly; never imply instant retrieval of cold data.

**Non-goals (v1)**
- In-browser file **preview** or **streaming**, and **browser-side download** (fetches land on the restore mount only).
- **Cross-job search** / global search.
- Any **edit/delete/move** of stored data (read + recover only).
- Touching the **frozen estimator** (`app/estimator/*`).

## Decisions (from Q&A, 2026-09-22)

| # | Decision |
|---|----------|
| Purpose | **Browse + restore (both):** browse the tree/history, select a file/folder/version, and restore or download it from there. |
| Restic depth | **On-demand + cache:** the first time a snapshot is opened, run `restic ls --json <snap>` once and cache the full listing per snapshot-id (snapshots are immutable → the cache never goes stale). Archive/vfiles list live (cheap). |
| Placement | **Top-level `Explore` nav:** `/explore` lists jobs → `/explore/<job>` browses. |
| Download target | **To the restore mount (RESTORE_ROOT)** on the box; cold objects thaw first via the existing flow. No browser streaming. |
| Action confirm | **CSRF only** for explorer restore/download actions — no typed-name confirm. A single-node fetch is non-destructive (read-only from S3, writes only into the isolated restore mount); the typed-name guard remains on the existing full-job restore flow. |

## Architecture

No change to how data is stored or backed up. Three additions: (1) read-only per-type
**browse** listing in the runner, (2) GUI **Explore** routes + screens, (3) a
CSRF-guarded **action** route that reuses the existing restore/thaw machinery.

### 1. Per-type browse backend — `scripts/restore.sh` + `app/engine/`

Add read-only listing beside the existing `restore.sh <job> list` (same
no-lock/no-run-record contract as `list`/`thaw-status`). Each returns **one directory
level** as JSON: `{"path": "<dir>", "entries": [{"name","kind":"dir|file","size","storage_class","modified"}...]}`
(fields absent when a type can't supply them).

- **archive** — `restore.sh <job> browse [<relpath>] --json` → `rclone lsjson s3:<bucket>/media/<job>/<relpath>` (rclone `lsjson` is one level by default). Maps rclone's `Name/Size/IsDir/Tier/ModTime` into the entry shape. Free/fast; no cache.
- **versioned-files** — `restore.sh <job> browse [<relpath>] --json` delegates to `python3 -m app.engine.vfiles browse <job> <relpath> [--json]`, a new subcommand that folds the flat catalog relpaths into one directory level (see §catalog). Adds, per file entry, `versions: [{version_id, uploaded_at, storage_class, size, deleted}]` (newest-first) so the UI can offer as-of restore. Free/fast (local SQLite, read-only).
- **versioned (restic)** — `restore.sh <job> browse --snapshot <snap> [<relpath>] --json`. On first call for `<snap>`, run `restic ls --json <snap> --tag <job>` **once**, write the full recursive listing to a cache file, then fold+serve the requested level from that cache. Subsequent calls (any path within the same snapshot) read the cache. Snapshot ids are immutable, so the cache is permanent. Snapshot enumeration itself reuses the existing `restic snapshots --tag <job> --json` (already cached at `state/<job>.points.json`).

**Restic browse cache:** `"$CACHE_DIR/state/<job>.browse/<snapshot-id>.json"` = the full
`restic ls --json` array for that snapshot. Written atomically (temp+rename). A dir-tree
fold over it is done in Python (`app/engine/browse.py`) so bash stays a thin shell around
the tool. **Cleanup:** the cache is a **directory** (`state/<job>.browse/`), not a suffixed
file, so `jobs_io._remove_job_caches` must `shutil.rmtree(state/<job>.browse/)` on job delete
(alongside the existing `rmtree(logs/runs/<job>)`) — the `_CACHE_STATE_SUFFIXES` file loop
won't catch a directory.

### 2. Tree folding — `app/engine/browse.py` (new) + `app/engine/catalog.py`

A small pure module that turns a **flat path list** (from restic ls cache or the vfiles
catalog) into one directory level:

- `fold_level(paths, cur_dir) -> {"dirs": [name...], "files": [{"name","path", ...}...]}`:
  dirs = distinct next segment of every path strictly under `cur_dir`; files = paths whose
  parent == `cur_dir`. Pure, unit-testable, no I/O.
- `catalog.browse(conn, cur_dir) -> ...` (new, read-only): uses `fold_level` over
  `SELECT path,... WHERE is_current=1` for the level, and `catalog.versions(conn, path)`
  (exists today) for a file's history. Opened read-only (`file:...?mode=ro`, uri=True),
  degrade-to-empty on any error — mirror `points.py:_catalog_totals`.

Archive doesn't need folding (rclone `lsjson` is already one level); its handler maps
rclone JSON straight into the entry shape.

### 3. GUI — routes (`app/gui/routes.py`) + templates + nav

- `GET /explore` → `explore_index`: lists jobs (name, type, measured size from the usage cache if present) → `explore_index.html`. Pure GET.
- `GET /explore/<job>` → `explore_job`: renders `explore.html`. Query params: `path` (dir, default root), `snapshot` (versioned; default = latest snapshot id), `asof` (vfiles; optional). 404 unknown job. For versioned, includes the snapshot picker (from the points cache); for archive/vfiles, no snapshot control. First render may show a "loading contents…" state for restic while the ls cache warms.
- `GET /explore/<job>/list.json` → `explore_list`: the read-only listing endpoint the page fetches for lazy per-level loading (like `/jobs/<job>/progress.json` and `/logs` — GET, no CSRF, no mutation). Params `path`/`snapshot`. Calls the runner's `browse` (or the Python browse directly for vfiles) and returns the entry JSON. On a cold-warming or restic-cache-building state, returns `{"building": true}` so the UI can show a spinner and retry.
- `POST /explore/<job>/get` → `explore_get`: **CSRF only** (per decision). Validates the selected `path`/`snapshot`/`version`/`asof`, confines the path to the job's scope, confines the target to RESTORE_ROOT (`ops.validate_target`), checks the job lock (`ops.ensure_free` → 409), then `ops.launch` the exact `restore.sh` argv (built by the existing `_restore_argv`, now given the explorer's `path`/`scope`) and redirect to the run-record page. If the selected node is **cold** (GLACIER/DEEP_ARCHIVE) and not yet thawed, route to the existing **thaw** action instead (issue warm-up, land on the thaw-status view) — never a silent no-op.

**Reuse, don't fork:** `explore_get` fills the same `path`/`scope=file` fields the restore
confirm pipeline already threads (dead wiring today) and calls the same `ops.launch` /
`_restore_argv` / target-confinement code — it just swaps the typed-name confirm for a plain
CSRF check.

- **Nav:** add an **Explore** item to the shell nav (`base.html`), following the existing nav-item pattern (Board · Cost · Activity · Setup → + Explore). Also add a per-job "Browse contents →" link on the job page (`job.html`) pointing at `/explore/<job>` as a convenience.

### 4. UI screens

- `explore_index.html` (`screen dense`): one row/card per job — name, plain-type label, measured size — linking to `/explore/<job>`.
- `explore.html` (`screen dense`): a breadcrumb of the current path; a directory listing (dirs first, then files) showing name, size, storage-class token, modified date; for **vfiles**, a per-file version affordance (pick a version / as-of); for **versioned**, a snapshot `<select>` at the top; a **cold** badge on cold nodes. Each file/folder row carries a CSRF `<form>` POSTing to `/explore/<job>/get` with the node's `path` (+ `snapshot`/`version`/`asof` as applicable) and a target-path input (defaulting under RESTORE_ROOT). Lazy level loading via `list.json` + `app.js` (guarded progressive enhancement; the page also works with plain full-page navigations using the `?path=` query param as a no-JS fallback).

## Data flow per type (summary)

| Type | List a level | History | Restore/download argv (existing restore.sh) |
|---|---|---|---|
| archive | `rclone lsjson media/<job>/<path>` (live) | none (current copy) | `restore.sh <job> download <path> <target>` |
| versioned-files | catalog SQLite fold (live, read-only) | per-path versions + `--asof` | `restore.sh <job> <path> <target> [--asof TS]` |
| versioned (restic) | `restic ls --json <snap>` once → cache → fold | snapshots | `restore.sh <job> restore <snap> <target> --include <path>` |

## Security / error handling

- **Path confinement:** a browse/action `path` is a job-relative POSIX path; reject absolute paths, `..` segments, and empty/`.`-escapes before use (a `_safe_rel(path)` helper mirroring `fsbrowse.safe_resolve`'s confinement-first discipline — `PathError → 404`, never echo the bad path). archive/vfiles paths are confined under `media/<job>/`; restic paths are within the chosen snapshot. A job can never browse another job's data or restic `appdata/` internals (versioned browse always goes through `restic … --tag <job>`, never raw S3).
- **Actions:** CSRF-verified (`security.verify_csrf`); target confined to RESTORE_ROOT (never the live source) via `ops.validate_target`; single-flight lock via `ops.ensure_free`.
- **Cold storage:** listing metadata always works (ListBucket/HEAD/catalog, independent of restore state), so browsing cold data is fine; a fetch of a cold, un-thawed node routes through the existing thaw flow (issue warm-up + thaw-status), and the UI badges cold nodes — never implying an instant download.
- **Degradation:** a listing failure (tool error, missing cache, unreachable S3) renders an inline "couldn't read contents" state, never a 500; read-only endpoints never mutate.

## Testing

- **`browse.fold_level`** (pure): unit tests — nested paths fold to the right dirs/files per level; root and deep levels; no leakage across siblings.
- **`catalog.browse`**: real temp SQLite catalog — folds current files, returns per-path version history, read-only (never writes), degrades on a bad DB.
- **restore.sh `browse`** (bats): stub `rclone`/`restic`; archive returns a level from `lsjson`; versioned runs `restic ls --json` **once** then serves from cache on a second call (assert the stub ran once); confinement rejects `..`/absolute; `--json` shape stable.
- **Routes** (pytest, real test client): `GET /explore` lists jobs; `GET /explore/<job>` renders per type; `GET /explore/<job>/list.json` returns a level (and `{"building":true}` while a restic cache warms); `POST /explore/<job>/get` requires CSRF (empty → 400), confines the path (escape → 404) and target (→ RESTORE_ROOT), 409 on a busy lock, and routes a cold node to thaw; unknown job → themed 404.
- **Job-delete cleanup:** `jobs_io.delete` also removes `state/<job>.browse/`.
- Estimator hash-guard stays green.

## Open questions / future

- v2: in-browser preview/stream of small files; browser download; cross-job search; a "restore selection" multi-select cart.
- Restic browse cache warming could move to a background prefetch after a backup; v1 warms on first open.
- vfiles as-of browsing (show the tree as it was at a timestamp) is possible from the catalog; v1 browses the current set + per-file history.
