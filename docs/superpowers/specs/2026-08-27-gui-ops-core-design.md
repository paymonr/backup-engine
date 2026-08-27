# GUI Ops-Core — Design Spec

**Date:** 2026-08-27
**Status:** Approved (brainstorming) — ready for implementation plan
**Parent spec:** `2026-08-26-unraid-s3-backup-redesign.md` §9 (GUI), §17 Phase 2
**Slice:** First bounded piece of the Phase-2 GUI — the web-app skeleton + the config editor + the run/status/log-tail screen. The other §9 screens are later slices (see §11 Roadmap).

---

## 1. Overview & role

A small **Flask** web app, packaged into the existing container, that lets an operator configure and run the backup engine without the CLI. Two screens:

- **Config editor** — reads/writes the three mounted config files (`backup.env`, `includes-media.txt`, `secrets.env`), the single source of truth.
- **Run / status / logs** — trigger a backup now, see the last-run outcome per pipeline, and tail the live log.

**Invariant (spec §9):** the GUI never reimplements backup logic. "Run now" shells out to the same `scripts/backup-appdata.sh` / `scripts/backup-media.sh` the scheduler invokes, so GUI and headless behavior are identical by construction.

## 2. Goals & non-goals

### Goals
- Stand up the reusable web-app skeleton (Flask app factory, base layout, static assets, production server, container process model) that every later GUI screen builds on.
- Config editor for all three files, with **write-only** handling of secrets (never render existing secret values).
- Run-now + last-run status + live log tail, reading the engine's existing run-state JSON and log file.
- Package into the image with a `GUI_ENABLED` toggle; document usage + the no-auth security posture.

### Non-goals (this slice)
- No restore wizard, media-dir picker, provisioning wizard, or estimator screen (later slices — §11).
- **No built-in authentication.** The GUI is designed to sit behind the adopter's own reverse proxy / SSO (spec §9). Native OIDC is a roadmap item (§11).
- No rich per-run history store — the engine persists only last-run state JSON; "history" here is last-run status + the accumulating log. A per-run history log is deferred.
- No new backup/restore behavior — this slice only *drives and observes* the existing engine.

## 3. Resolved decisions

| Decision | Choice | Why |
|---|---|---|
| Framework | **Flask** (Jinja2, server-rendered) | Page-rendering ops panel that shells out; minimal pure-Python deps for the Alpine image |
| WSGI server | **waitress** | Pure-Python production server, no C deps, simple; Flask's dev server is not for production |
| Secrets in the editor | **Write-only** fields | Never render AWS keys / restic password; blank = unchanged, submit = overwrite. Plus manual-creation instructions |
| Auth | **None built in** (behind reverse proxy / SSO) | Spec §9; OIDC is a roadmap item |
| Process model | supercronic (background) + waitress (foreground), gated on `GUI_ENABLED` | One container keeps scheduler + GUI; foreground server owns signals/logs |
| History | Last-run state JSON + log tail | The engine persists only last-run state; richer history deferred |

## 4. Architecture & units

```
app/gui/
  __init__.py      # create_app(config) -> Flask   (application factory)
  config_io.py     # read/write backup.env + includes-media.txt + secrets.env (write-only secrets)
  runner.py        # trigger_backup(pipeline), read_state(), tail_log(n)  — shells out / reads /cache
  routes.py        # the view functions / blueprint
  security.py      # CSRF token issue+verify (Flask session, no extra dep)
  templates/       # base.html, config.html, status.html
  static/          # style.css, app.js (log-tail polling)
  server.py        # `python3 -m app.gui.server` → waitress.serve(create_app())
tests/gui/
  conftest.py      # Flask test client + a tmp CONFIG_DIR/CACHE_DIR fixture
  test_config_io.py
  test_runner.py
  test_routes.py
```

**Boundaries:**
- `config_io.py` is the only reader/writer of the config files. It reuses the same safe KEY=VALUE parsing approach as the engine (no `source`/`exec`).
- `runner.py` is the only module that launches subprocesses and reads `/cache` state/logs.
- `routes.py`/`templates` render; they never touch files or subprocesses directly — they call `config_io`/`runner`.
- The app is created by a **factory** `create_app()` so tests construct an isolated instance with a temp config/cache dir.

## 5. Config editor

**`backup.env`** — a structured form of the documented knobs. The **committed `config/backup.env.example` is the template and the single source of truth** for the field set, ordering, and comments: `config_io` parses it to discover the editable keys (each `KEY=value` line, commented-out optional keys included) and their surrounding comments. The form is rendered from that; the current `backup.env` (if present) supplies the live values. On save, `config_io` **rewrites `backup.env` from `config/backup.env.example`** with the submitted values substituted per key — so the example's comments/structure are preserved and the editor never drifts from the canonical template. (Consequence: hand-added comments in a user's own `backup.env` are not preserved across a GUI save — the comments always come from the example; documented.)

**`includes-media.txt`** — a raw `<textarea>` (rclone filter list; order matters).

**`secrets.env`** — three write-only fields: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `RESTIC_PASSWORD`. Rendered blank; the page shows only **set / not-set** per key plus the file's mode (warns if not `600`). On submit, only non-blank fields overwrite; the file is (re)written `chmod 600`.

**Manual instructions.** A "prefer to create it by hand?" panel shows the exact shell to write `secrets.env` (copy the `.example`, `chmod 600`, edit) — mirrored in the README.

**Validation (light, Python-side):** required fields present (`AWS_REGION`, `S3_BUCKET`), storage classes in the known set, schedules look like 5-field cron, numeric fields numeric. Errors re-render the form with messages. Deep validation still happens at run time in the engine (which validates + failure-notifies).

## 6. Run / status / logs

- **Run now** — `POST /run/<pipeline>` (`appdata`|`media`) → `runner.trigger_backup` launches `scripts/backup-<pipeline>.sh` as a **detached background subprocess** and returns immediately (flash "started"). The engine's `flock` already blocks concurrent runs; if a run is in progress the state/lock reflects it and the button is disabled.
- **Status** — `runner.read_state()` reads `/cache/state/appdata.json` + `media.json` (`last_run`, `outcome`, `snapshot_id`/`mode`, `duration_s`); the page shows per-pipeline last outcome. Missing state = "never run".
- **Live log tail** — `GET /logs?tail=N` returns the last N lines of `$CACHE_DIR/logs/backup-engine.log` (plain text); `static/app.js` polls it every few seconds and appends. No websockets.

## 7. Security posture

- **No built-in auth** (spec §9). The base layout and README carry a clear banner: *"This GUI has no authentication — put it behind a reverse proxy / SSO and never expose it directly to the internet."*
- **Secrets are write-only** — existing values are never sent to the browser.
- **CSRF** — state-changing POSTs (config save, run) carry a token issued into the Flask session and verified server-side (`security.py`, using Flask's bundled `itsdangerous`; no new dependency). Session cookie is `HttpOnly`, `SameSite=Lax`.
- **Bind** — the server binds the configured `GUI_PORT`; the README notes it should be reachable only via the proxy.
- The GUI process runs as the same container user and only reads/writes `/config` and `/cache` — the same surface the engine already has.

## 8. Entrypoint & process model

`entrypoint.sh` currently `exec`s supercronic. New flow:
1. `prepare()` (validate, render config, version banner) — unchanged.
2. If `GUI_ENABLED` (default `true`): start supercronic in the **background**, then `exec` the GUI (`python3 -m app.gui.server`) in the **foreground** (so it owns PID 1 signals + stdout).
3. If `GUI_ENABLED=false`: current behavior — `exec` supercronic (scheduler-only, headless).
4. `RUN_ONCE=<pipeline>` (existing) still runs a single pipeline and exits, regardless of GUI.

`GUI_ENABLED` and `GUI_PORT` are read from config (defaults `true` / `8099`). Backgrounded supercronic's failure should not be silent — the entrypoint logs its start and the GUI surfaces scheduler liveness best-effort (a later enhancement; not blocking).

## 9. Packaging / CI / docs

- **Dockerfile** — add `flask` + `waitress` via `pip` (pure Python, no C build). `COPY app/` already carries `app/gui`. Expose stays as-is (host maps `GUI_PORT`; the CA template already publishes 8099).
- **CI** — a `gui` job: `pip install flask waitress pytest`, `python3 -m pytest tests/gui/`. No browser (Flask test client).
- **README** — GUI section (reach it at `http://host:8099`, the no-auth warning, config editor + run/status/logs walkthrough, manual `secrets.env` instructions), and a new **Roadmap** section listing the deferred items (§11).
- **CA template** — `WebUI` already points at the GUI port; no change needed this slice.

## 10. Testing strategy

`pytest` via the Flask **test client**, fully offline, against a temp `CONFIG_DIR`/`CACHE_DIR`:

- **`config_io`** — the editable key set is derived from `config/backup.env.example`; saving regenerates `backup.env` from that example with submitted values (assert the example's comment lines are present in the output and values round-trip); `includes-media.txt` raw round-trip; **secrets write-only**: existing secret never appears in a rendered form, blank field leaves it unchanged, non-blank overwrites, file mode is `600`.
- **`runner`** — `read_state()` parses present/absent state JSON; `tail_log(n)` returns the last n lines; `trigger_backup` invokes the right script path (subprocess mocked/stubbed — assert the command, don't actually run a backup).
- **`routes`** — GET config/status render 200; POST config save writes files and redirects; POST run calls `trigger_backup`; CSRF-less POST is rejected; `/logs` returns the tail. Secret values never appear in any rendered response.

## 11. Roadmap / deferred (documented in README)

Future GUI work, tracked in the README's Roadmap section:
- **Restore wizard** — the guided both-tier restore screen (incl. cold-thaw flow).
- **Media-dir picker** — browse the read-only media mount to build the include-list.
- **Provisioning wizard** — the three-mode setup (automated / scripted / guided-manual), incl. write-only runtime-key entry.
- **Estimator screen** — interactive what-if wrapping the existing `estimate()` module.
- **OIDC authentication** — native OpenID Connect login, so the GUI can be exposed without relying solely on an external proxy.
- **Per-run history** — a persisted history of runs beyond the last-run state JSON.

## 12. Out of scope

- Any change to backup/restore engine behavior (this slice only drives + observes).
- Multi-user / RBAC (single operator behind a proxy).
- TLS termination (the reverse proxy's job).
- Non-AWS backends (Phase 4).
