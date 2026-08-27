# GUI Ops-Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a small Flask GUI, packaged into the existing container, with a config editor (all three mounted files, write-only secrets) and a run/status/log-tail screen that drives the existing engine scripts.

**Architecture:** A Flask app-factory (`app/gui/`) with three thin layers — `config_io` (the only reader/writer of the mounted config files), `runner` (the only subprocess launcher / `/cache` reader), and `routes` + Jinja templates. The container entrypoint runs supercronic in the background and the GUI (via waitress) in the foreground, gated on `GUI_ENABLED`. The GUI never reimplements backup logic — "run now" shells out to `scripts/backup-*.sh`.

**Tech Stack:** Python 3, **Flask** (Jinja2, server-rendered) + **waitress** (pure-Python WSGI server); `pytest` (dev/CI) with the Flask test client. No JS framework — one small `app.js` polls for the log tail.

**Spec:** `docs/superpowers/specs/2026-08-27-gui-ops-core-design.md`

## Global Constraints

- **GUI never reimplements engine behavior.** Run-now shells out to `scripts/backup-appdata.sh` / `scripts/backup-media.sh`; status/logs read the engine's existing `/cache/state/*.json` and `/cache/logs/backup-engine.log`. (§1, §6)
- **Secrets are write-only.** Existing `secrets.env` values are never rendered to the browser or returned by any endpoint; blank field = unchanged, non-blank = overwrite; the file is written mode `600`. (§5, §7)
- **`config/backup.env.example` is the template and single source of truth** for `backup.env`'s editable key set, ordering, and comments. Saving regenerates `backup.env` from that example with submitted values substituted per key. (§5)
- **No built-in auth.** The GUI assumes a reverse proxy / SSO in front; every page shows a "no auth — do not expose directly" banner. State-changing POSTs carry a CSRF token (stdlib `secrets` + Flask session; no extra dependency). Session cookie `HttpOnly` + `SameSite=Lax`. (§7)
- **`config_io` is the only config-file reader/writer; `runner` is the only subprocess launcher / cache reader.** Routes/templates call those modules, never the filesystem or subprocess directly. (§4)
- **Pure-Python deps only** (`flask`, `waitress`) — no C build, fine for Alpine. `pytest` is CI-only. (§9)
- **Offline, deterministic tests** via the Flask test client against a temp `CONFIG_DIR`/`CACHE_DIR` and the repo's `config/backup.env.example` as the template. No network, no real backups (subprocess stubbed). (§10)
- **`GUI_ENABLED=true` default; `GUI_PORT=8099` default.** `GUI_ENABLED=false` keeps the old scheduler-only headless behavior. (§8)
- **Python style:** `from __future__ import annotations`, type hints, small focused modules.

---

### Task 1: `config_io` — read/write the three config files

The only reader/writer of the mounted config files. Regenerates `backup.env` from the bundled example template; handles `includes-media.txt`; write-only secrets.

**Files:**
- Create: `app/gui/__init__.py` (empty package marker for now)
- Create: `app/gui/config_io.py`
- Create: `tests/gui/__init__.py` (empty)
- Create: `tests/gui/conftest.py`
- Test: `tests/gui/test_config_io.py`

**Interfaces:**
- Consumes: `config/backup.env.example` (template) + the mounted config dir.
- Produces:
  - `SECRET_KEYS: tuple[str, ...]` = `("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "RESTIC_PASSWORD")`.
  - `template_keys(template_path) -> list[str]` — ordered editable keys parsed from the example (both `KEY=` and `#KEY=` lines).
  - `read_backup_env(config_dir) -> dict[str, str]` — live values (`{}` if the file is absent).
  - `write_backup_env(template_path, config_dir, values: dict[str, str]) -> None` — regenerate `backup.env` from the template, substituting non-empty `values[KEY]` per matching line.
  - `read_includes(config_dir) -> str` / `write_includes(config_dir, text: str) -> None`.
  - `secrets_status(config_dir) -> dict[str, bool]` — per `SECRET_KEYS`, whether set.
  - `secrets_mode(config_dir) -> str | None` — octal file mode (e.g. `"600"`) or `None` if absent.
  - `write_secrets(config_dir, values: dict[str, str]) -> None` — non-empty values overwrite; blanks keep existing; file written mode `600`.

- [ ] **Step 1: Write the test conftest**

```python
# tests/gui/conftest.py
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "config" / "backup.env.example"

@pytest.fixture
def template_path() -> str:
    return str(TEMPLATE)

@pytest.fixture
def dirs(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    cache = tmp_path / "cache"; (cache / "state").mkdir(parents=True); (cache / "logs").mkdir()
    return {"config": str(cfg), "cache": str(cache)}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/gui/test_config_io.py
from pathlib import Path
import os
from app.gui import config_io as cio

def test_template_keys_include_optional_commented_keys(template_path):
    keys = cio.template_keys(template_path)
    assert "AWS_REGION" in keys and "S3_BUCKET" in keys
    assert "S3_ENDPOINT" in keys          # commented-out optional key in the example
    assert "APPDATA_STORAGE_CLASS" in keys

def test_write_backup_env_regenerates_from_template_preserving_comments(template_path, dirs):
    cio.write_backup_env(template_path, dirs["config"], {"AWS_REGION": "us-west-2", "S3_BUCKET": "mybucket"})
    out = Path(dirs["config"], "backup.env").read_text()
    assert "AWS_REGION=us-west-2" in out
    assert "S3_BUCKET=mybucket" in out
    # a comment line from the example survives
    assert any(line.startswith("#") for line in out.splitlines())
    # round-trips through the reader
    assert cio.read_backup_env(dirs["config"])["AWS_REGION"] == "us-west-2"

def test_write_backup_env_ignores_blank_values(template_path, dirs):
    cio.write_backup_env(template_path, dirs["config"], {"S3_ENDPOINT": ""})
    out = Path(dirs["config"], "backup.env").read_text()
    # blank S3_ENDPOINT stays as the template's commented line, not an empty assignment
    assert "\nS3_ENDPOINT=\n" not in out

def test_includes_round_trip(dirs):
    cio.write_includes(dirs["config"], "+ /comics/**\n- **\n")
    assert cio.read_includes(dirs["config"]) == "+ /comics/**\n- **\n"

def test_secrets_are_write_only(dirs):
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh", "RESTIC_PASSWORD": "pw"})
    status = cio.secrets_status(dirs["config"])
    assert status == {"AWS_ACCESS_KEY_ID": True, "AWS_SECRET_ACCESS_KEY": True, "RESTIC_PASSWORD": True}
    # blank leaves existing unchanged; non-blank overwrites
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "new", "RESTIC_PASSWORD": ""})
    from app.gui.config_io import _parse_env
    vals = _parse_env(Path(dirs["config"], "secrets.env").read_text())
    assert vals["AWS_ACCESS_KEY_ID"] == "AKIA"       # unchanged
    assert vals["AWS_SECRET_ACCESS_KEY"] == "new"    # overwritten
    assert cio.secrets_mode(dirs["config"]) == "600"

def test_secrets_status_absent_file(dirs):
    assert cio.secrets_status(dirs["config"]) == {k: False for k in cio.SECRET_KEYS}
    assert cio.secrets_mode(dirs["config"]) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/gui/test_config_io.py -v`
Expected: FAIL — `app.gui.config_io` does not exist.

- [ ] **Step 4: Implement `app/gui/config_io.py`**

```python
# app/gui/config_io.py — the ONLY reader/writer of the mounted config files.
from __future__ import annotations
from pathlib import Path
import re

SECRET_KEYS: tuple[str, ...] = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "RESTIC_PASSWORD")
_KEY_RE = re.compile(r"^\s*#?\s*([A-Z0-9_]+)=")

def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.split("#", 1)[0].strip().strip('"').strip("'")
    return out

def template_keys(template_path: str) -> list[str]:
    keys: list[str] = []
    for line in Path(template_path).read_text().splitlines():
        m = _KEY_RE.match(line)
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys

def read_backup_env(config_dir: str) -> dict[str, str]:
    p = Path(config_dir, "backup.env")
    return _parse_env(p.read_text()) if p.exists() else {}

def write_backup_env(template_path: str, config_dir: str, values: dict[str, str]) -> None:
    out: list[str] = []
    for line in Path(template_path).read_text().splitlines():
        m = _KEY_RE.match(line)
        if m and values.get(m.group(1), "") != "":
            out.append(f"{m.group(1)}={values[m.group(1)]}")
        else:
            out.append(line)
    Path(config_dir, "backup.env").write_text("\n".join(out) + "\n")

def read_includes(config_dir: str) -> str:
    p = Path(config_dir, "includes-media.txt")
    return p.read_text() if p.exists() else ""

def write_includes(config_dir: str, text: str) -> None:
    Path(config_dir, "includes-media.txt").write_text(text)

def secrets_status(config_dir: str) -> dict[str, bool]:
    p = Path(config_dir, "secrets.env")
    vals = _parse_env(p.read_text()) if p.exists() else {}
    return {k: bool(vals.get(k)) for k in SECRET_KEYS}

def secrets_mode(config_dir: str) -> str | None:
    p = Path(config_dir, "secrets.env")
    return oct(p.stat().st_mode & 0o777)[2:] if p.exists() else None

def write_secrets(config_dir: str, values: dict[str, str]) -> None:
    p = Path(config_dir, "secrets.env")
    existing = _parse_env(p.read_text()) if p.exists() else {}
    for k in SECRET_KEYS:
        if values.get(k, "") != "":
            existing[k] = values[k]
    body = "# backup-engine secrets — managed by the GUI (mode 600).\n"
    body += "".join(f"{k}={existing[k]}\n" for k in SECRET_KEYS if k in existing)
    p.write_text(body)
    p.chmod(0o600)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/gui/test_config_io.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add app/gui/__init__.py app/gui/config_io.py tests/gui/
git commit -m "feat(gui): config_io — read/write config files, example-template backup.env, write-only secrets"
```

---

### Task 2: `runner` — trigger backups, read state, tail logs

The only module that launches subprocesses and reads `/cache`.

**Files:**
- Create: `app/gui/runner.py`
- Test: `tests/gui/test_runner.py`

**Interfaces:**
- Consumes: `scripts/backup-*.sh`, `$CACHE_DIR/state/*.json`, `$CACHE_DIR/logs/backup-engine.log`.
- Produces:
  - `PIPELINES: dict[str, str]` = `{"appdata": "backup-appdata.sh", "media": "backup-media.sh"}`.
  - `read_state(cache_dir, pipeline) -> dict | None` — parsed state JSON, or `None` if absent/unparseable.
  - `tail_log(cache_dir, n=200) -> str` — last `n` lines of the engine log (`""` if absent).
  - `trigger_backup(scripts_dir, pipeline, env=None) -> None` — launches the pipeline script as a detached background process. Raises `ValueError` on an unknown pipeline.

- [ ] **Step 1: Write the failing tests**

```python
# tests/gui/test_runner.py
import json
from pathlib import Path
import pytest
from app.gui import runner

def test_read_state_present(dirs):
    Path(dirs["cache"], "state", "appdata.json").write_text(json.dumps({"outcome": "success", "snapshot_id": "abc"}))
    st = runner.read_state(dirs["cache"], "appdata")
    assert st["outcome"] == "success" and st["snapshot_id"] == "abc"

def test_read_state_absent_returns_none(dirs):
    assert runner.read_state(dirs["cache"], "media") is None

def test_tail_log_returns_last_n(dirs):
    Path(dirs["cache"], "logs", "backup-engine.log").write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    assert runner.tail_log(dirs["cache"], n=3).splitlines() == ["line7", "line8", "line9"]

def test_trigger_backup_launches_correct_script(dirs, monkeypatch):
    calls = {}
    def fake_popen(cmd, **kw):
        calls["cmd"] = cmd; calls["kw"] = kw
        class P: pass
        return P()
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    runner.trigger_backup("/app/scripts", "media")
    assert calls["cmd"] == ["bash", "/app/scripts/backup-media.sh"]
    assert calls["kw"].get("start_new_session") is True

def test_trigger_backup_unknown_pipeline_raises(dirs):
    with pytest.raises(ValueError):
        runner.trigger_backup("/app/scripts", "nope")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/gui/test_runner.py -v`
Expected: FAIL — `app.gui.runner` missing.

- [ ] **Step 3: Implement `app/gui/runner.py`**

```python
# app/gui/runner.py — the ONLY subprocess launcher / cache reader.
from __future__ import annotations
from pathlib import Path
import json
import os
import subprocess

PIPELINES: dict[str, str] = {"appdata": "backup-appdata.sh", "media": "backup-media.sh"}

def read_state(cache_dir: str, pipeline: str) -> dict | None:
    p = Path(cache_dir, "state", f"{pipeline}.json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None

def tail_log(cache_dir: str, n: int = 200) -> str:
    p = Path(cache_dir, "logs", "backup-engine.log")
    if not p.exists():
        return ""
    return "\n".join(p.read_text(errors="replace").splitlines()[-n:])

def trigger_backup(scripts_dir: str, pipeline: str, env: dict | None = None) -> None:
    if pipeline not in PIPELINES:
        raise ValueError(f"unknown pipeline '{pipeline}'")
    script = str(Path(scripts_dir, PIPELINES[pipeline]))
    subprocess.Popen(
        ["bash", script],
        env=env or os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/gui/test_runner.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/gui/runner.py tests/gui/test_runner.py
git commit -m "feat(gui): runner — trigger backups (subprocess), read run-state, tail log"
```

---

### Task 3: App factory + CSRF + base skeleton

The Flask application factory, CSRF helpers, base layout with the no-auth banner, and an index route. Later tasks add view functions to the same blueprint.

**Files:**
- Create: `app/gui/security.py`
- Create: `app/gui/routes.py` (blueprint + index; extended in Tasks 4–5)
- Modify: `app/gui/__init__.py` (add `create_app`)
- Create: `app/gui/templates/base.html`
- Create: `app/gui/static/style.css`
- Test: `tests/gui/test_app.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `create_app(config: dict | None = None) -> Flask` — factory. Config keys: `CONFIG_DIR`, `CACHE_DIR`, `SCRIPTS_DIR`, `TEMPLATE_PATH` (the bundled `backup.env.example`), `SECRET_KEY`. Reads env defaults (`/config`, `/cache`, `/app/scripts`, `/app/config/backup.env.example`).
  - `security.issue_csrf() -> str` / `security.verify_csrf(token: str) -> bool` (Flask session + stdlib `secrets`).
  - `routes.bp` — the Flask `Blueprint`; `GET /` redirects to the config page.

- [ ] **Step 1: Write the failing tests**

```python
# tests/gui/test_app.py
import pytest
from app.gui import create_app

@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app):
    return app.test_client()

def test_index_redirects_to_config(client):
    r = client.get("/")
    assert r.status_code in (301, 302)
    assert "/config" in r.headers["Location"]

def test_no_auth_banner_present(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert b"no authentication" in r.data.lower()

def test_csrf_roundtrip(app):
    from app.gui import security
    with app.test_request_context():
        from flask import session
        token = security.issue_csrf()
        assert security.verify_csrf(token) is True
        assert security.verify_csrf("wrong") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/gui/test_app.py -v`
Expected: FAIL — `create_app` not defined.

- [ ] **Step 3: Implement `app/gui/security.py`**

```python
# app/gui/security.py — CSRF token via Flask session + stdlib secrets (no extra dependency).
from __future__ import annotations
import secrets
from flask import session

def issue_csrf() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]

def verify_csrf(token: str) -> bool:
    return bool(token) and secrets.compare_digest(token, session.get("_csrf", ""))
```

- [ ] **Step 4: Implement `app/gui/routes.py` (blueprint + index)**

```python
# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from flask import Blueprint, redirect, url_for

bp = Blueprint("gui", __name__)

@bp.get("/")
def index():
    return redirect(url_for("gui.config_page"))
```

(Note: `gui.config_page` is defined in Task 4. Until then this import target won't resolve at request time for `/`, but `/config` tests in this task hit the config route only after Task 4. For THIS task, temporarily point index at a stub: add the stub below and replace it in Task 4.)

Add a temporary config stub so this task's tests pass, to be replaced in Task 4:

```python
@bp.get("/config")
def config_page():
    from flask import render_template
    return render_template("config.html")
```

Create `app/gui/templates/config.html` as a minimal stub for this task (replaced in Task 4):

```html
{% extends "base.html" %}{% block body %}<h1>Config</h1>{% endblock %}
```

- [ ] **Step 5: Implement `create_app` in `app/gui/__init__.py`**

```python
# app/gui/__init__.py
from __future__ import annotations
import os
from flask import Flask

def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        CONFIG_DIR=os.environ.get("CONFIG_DIR", "/config"),
        CACHE_DIR=os.environ.get("CACHE_DIR", "/cache"),
        SCRIPTS_DIR=os.environ.get("SCRIPTS_DIR", "/app/scripts"),
        TEMPLATE_PATH=os.environ.get("BACKUP_ENV_TEMPLATE", "/app/config/backup.env.example"),
        SECRET_KEY=os.environ.get("GUI_SECRET_KEY") or os.urandom(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)
    from .routes import bp
    app.register_blueprint(bp)
    return app
```

- [ ] **Step 6: Implement `base.html` and `style.css`**

```html
<!-- app/gui/templates/base.html -->
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>backup-engine</title>
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head><body>
<div class="warn">⚠ This GUI has <strong>no authentication</strong>. Put it behind a reverse proxy / SSO — never expose it directly to the internet.</div>
<nav><a href="{{ url_for('gui.config_page') }}">Config</a> · <a href="{{ url_for('gui.status_page') }}">Run &amp; status</a></nav>
{% with msgs = get_flashed_messages() %}{% if msgs %}<ul class="flash">{% for m in msgs %}<li>{{ m }}</li>{% endfor %}</ul>{% endif %}{% endwith %}
<main>{% block body %}{% endblock %}</main>
</body></html>
```

(Note: `gui.status_page` is defined in Task 5; the nav link renders as a URL only when that route exists — add Task 5 before shipping. For this task, the `base.html` nav may reference it; to keep Task 3 tests green, define a stub `status_page` returning `""` in `routes.py`, replaced in Task 5.)

```python
# add to app/gui/routes.py (temporary stub, replaced in Task 5)
@bp.get("/status")
def status_page():
    return ""
```

```css
/* app/gui/static/style.css */
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 1rem auto; padding: 0 1rem; }
.warn { background: #ffe8e8; border: 1px solid #e0a0a0; padding: .5rem .75rem; border-radius: 6px; }
nav { margin: 1rem 0; } .flash { color: #175; } label { display:block; margin:.5rem 0 .15rem; font-weight:600; }
input, textarea, select { width: 100%; box-sizing: border-box; } textarea { min-height: 8rem; }
.status-ok { color: #175; } .status-fail { color: #a00; } pre.log { background:#111; color:#ddd; padding:.75rem; overflow:auto; max-height:24rem; }
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/gui/test_app.py -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Commit**

```bash
git add app/gui/security.py app/gui/routes.py app/gui/__init__.py app/gui/templates/ app/gui/static/ tests/gui/test_app.py
git commit -m "feat(gui): Flask app factory + CSRF + base layout (no-auth banner)"
```

---

### Task 4: Config editor screen

Replace the Task-3 config stub with the real editor: render the three files, save them (write-only secrets), CSRF-protected.

**Files:**
- Modify: `app/gui/routes.py` (real `config_page` GET + `config_save` POST)
- Create: `app/gui/templates/config.html` (replaces the stub)
- Test: `tests/gui/test_config_routes.py`

**Interfaces:**
- Consumes: `config_io` (all functions), `security` (CSRF), `app.config[CONFIG_DIR|TEMPLATE_PATH]`.
- Produces: `GET /config` (render form from template keys + live values + secret status); `POST /config` (save backup.env via template, includes, secrets; flash + redirect). Both CSRF-guarded on POST.

- [ ] **Step 1: Write the failing tests**

```python
# tests/gui/test_config_routes.py
import pytest
from pathlib import Path
from app.gui import create_app

@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app): return app.test_client()

def _csrf(client):
    client.get("/config")  # issues token into session
    from app.gui import security
    with client.session_transaction() as s:
        return s["_csrf"]

def test_config_get_renders_known_fields(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert b"AWS_REGION" in r.data and b"S3_BUCKET" in r.data

def test_config_save_writes_backup_env_and_redirects(client, dirs):
    token = _csrf(client)
    r = client.post("/config", data={"csrf": token, "AWS_REGION": "eu-west-1", "S3_BUCKET": "b"})
    assert r.status_code in (302, 303)
    assert "AWS_REGION=eu-west-1" in Path(dirs["config"], "backup.env").read_text()

def test_config_save_secrets_are_write_only(client, dirs):
    token = _csrf(client)
    client.post("/config", data={"csrf": token, "AWS_REGION": "us-east-1", "S3_BUCKET": "b",
                                 "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s", "RESTIC_PASSWORD": "p"})
    r = client.get("/config")
    # status shows "set" but never the value
    assert b"AKIA" not in r.data and b"RESTIC_PASSWORD" in r.data

def test_config_post_without_csrf_is_rejected(client):
    r = client.post("/config", data={"AWS_REGION": "x"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/gui/test_config_routes.py -v`
Expected: FAIL — real routes not implemented (stub renders no fields; no POST).

- [ ] **Step 3: Replace the config routes in `app/gui/routes.py`**

Remove the Task-3 `config_page` stub and add:

```python
from flask import render_template, request, redirect, url_for, flash, current_app, abort
from . import config_io, security

@bp.get("/config")
def config_page():
    cfg = current_app.config
    keys = config_io.template_keys(cfg["TEMPLATE_PATH"])
    values = config_io.read_backup_env(cfg["CONFIG_DIR"])
    fields = [{"key": k, "value": values.get(k, "")} for k in keys if k not in config_io.SECRET_KEYS]
    return render_template("config.html",
                           fields=fields,
                           includes=config_io.read_includes(cfg["CONFIG_DIR"]),
                           secret_keys=config_io.SECRET_KEYS,
                           secret_status=config_io.secrets_status(cfg["CONFIG_DIR"]),
                           secret_mode=config_io.secrets_mode(cfg["CONFIG_DIR"]),
                           csrf=security.issue_csrf())

@bp.post("/config")
def config_save():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
    cfg = current_app.config
    keys = [k for k in config_io.template_keys(cfg["TEMPLATE_PATH"]) if k not in config_io.SECRET_KEYS]
    config_io.write_backup_env(cfg["TEMPLATE_PATH"], cfg["CONFIG_DIR"],
                               {k: request.form.get(k, "") for k in keys})
    if "includes" in request.form:
        config_io.write_includes(cfg["CONFIG_DIR"], request.form["includes"])
    config_io.write_secrets(cfg["CONFIG_DIR"], {k: request.form.get(k, "") for k in config_io.SECRET_KEYS})
    flash("Configuration saved.")
    return redirect(url_for("gui.config_page"))
```

- [ ] **Step 4: Write `app/gui/templates/config.html`**

```html
{% extends "base.html" %}{% block body %}
<h1>Configuration</h1>
<form method="post" action="{{ url_for('gui.config_save') }}">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <h2>backup.env</h2>
  {% for f in fields %}
    <label for="{{ f.key }}">{{ f.key }}</label>
    <input id="{{ f.key }}" name="{{ f.key }}" value="{{ f.value }}">
  {% endfor %}
  <h2>includes-media.txt</h2>
  <textarea name="includes">{{ includes }}</textarea>
  <h2>Secrets <small>(write-only — leave blank to keep current)</small></h2>
  <p>Mode: {{ secret_mode or "file not present" }}{% if secret_mode and secret_mode != "600" %} <strong>(should be 600)</strong>{% endif %}</p>
  {% for k in secret_keys %}
    <label for="{{ k }}">{{ k }} — {{ "set" if secret_status[k] else "not set" }}</label>
    <input id="{{ k }}" name="{{ k }}" type="password" autocomplete="new-password" placeholder="{{ '••••• (unchanged)' if secret_status[k] else 'not set' }}">
  {% endfor %}
  <p><button type="submit">Save</button></p>
</form>
<details><summary>Prefer to create secrets by hand?</summary>
<pre>cp config/secrets.env.example /config/secrets.env
chmod 600 /config/secrets.env
$EDITOR /config/secrets.env   # set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, RESTIC_PASSWORD</pre>
</details>
{% endblock %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/gui/test_config_routes.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add app/gui/routes.py app/gui/templates/config.html tests/gui/test_config_routes.py
git commit -m "feat(gui): config editor screen (template-driven backup.env, write-only secrets, CSRF)"
```

---

### Task 5: Run / status / logs screen

Replace the Task-3 status stub with run-now buttons, last-run status, and the polling log tail.

**Files:**
- Modify: `app/gui/routes.py` (real `status_page` GET, `run` POST, `logs` GET)
- Create: `app/gui/templates/status.html`
- Create: `app/gui/static/app.js`
- Test: `tests/gui/test_status_routes.py`

**Interfaces:**
- Consumes: `runner` (all functions), `security` (CSRF), `app.config[CACHE_DIR|SCRIPTS_DIR]`.
- Produces: `GET /status` (per-pipeline last-run + log panel); `POST /run/<pipeline>` (CSRF-guarded → `runner.trigger_backup`, flash, redirect); `GET /logs?tail=N` (plain-text tail).

- [ ] **Step 1: Write the failing tests**

```python
# tests/gui/test_status_routes.py
import json
import pytest
from pathlib import Path
from app.gui import create_app, runner

@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app): return app.test_client()

def _csrf(client):
    client.get("/status")
    with client.session_transaction() as s:
        return s["_csrf"]

def test_status_shows_last_run(client, dirs):
    Path(dirs["cache"], "state", "appdata.json").write_text(json.dumps({"outcome": "success", "snapshot_id": "abc123", "duration_s": 5}))
    r = client.get("/status")
    assert r.status_code == 200
    assert b"success" in r.data and b"abc123" in r.data

def test_run_triggers_backup(client, monkeypatch):
    called = {}
    monkeypatch.setattr(runner, "trigger_backup", lambda scripts, pipeline, env=None: called.setdefault("p", pipeline))
    token = _csrf(client)
    r = client.post("/run/media", data={"csrf": token})
    assert r.status_code in (302, 303)
    assert called["p"] == "media"

def test_run_unknown_pipeline_404(client):
    token = _csrf(client)
    assert client.post("/run/bogus", data={"csrf": token}).status_code == 404

def test_run_without_csrf_rejected(client):
    assert client.post("/run/media", data={}).status_code == 400

def test_logs_returns_tail(client, dirs):
    Path(dirs["cache"], "logs", "backup-engine.log").write_text("a\nb\nc\n")
    r = client.get("/logs?tail=2")
    assert r.status_code == 200 and r.data.decode().splitlines() == ["b", "c"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/gui/test_status_routes.py -v`
Expected: FAIL — real status/run/logs routes not implemented.

- [ ] **Step 3: Replace the status stub in `app/gui/routes.py`**

Remove the Task-3 `status_page` stub and add:

```python
from flask import Response
from . import runner

@bp.get("/status")
def status_page():
    cfg = current_app.config
    states = {p: runner.read_state(cfg["CACHE_DIR"], p) for p in runner.PIPELINES}
    return render_template("status.html", states=states, csrf=security.issue_csrf())

@bp.post("/run/<pipeline>")
def run(pipeline):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
    if pipeline not in runner.PIPELINES:
        abort(404)
    runner.trigger_backup(current_app.config["SCRIPTS_DIR"], pipeline)
    flash(f"Started {pipeline} backup.")
    return redirect(url_for("gui.status_page"))

@bp.get("/logs")
def logs():
    n = request.args.get("tail", default=200, type=int)
    return Response(runner.tail_log(current_app.config["CACHE_DIR"], n), mimetype="text/plain")
```

- [ ] **Step 4: Write `status.html` and `app.js`**

```html
{% extends "base.html" %}{% block body %}
<h1>Run &amp; status</h1>
{% for pipeline, st in states.items() %}
<section>
  <h2>{{ pipeline }}</h2>
  {% if st %}<p class="status-{{ 'ok' if st.outcome == 'success' else 'fail' }}">
    last run: {{ st.outcome }}{% if st.snapshot_id %} · snapshot {{ st.snapshot_id }}{% endif %}{% if st.duration_s is defined %} · {{ st.duration_s }}s{% endif %} · {{ st.last_run }}
  </p>{% else %}<p>never run</p>{% endif %}
  <form method="post" action="{{ url_for('gui.run', pipeline=pipeline) }}">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit">Run {{ pipeline }} now</button>
  </form>
</section>
{% endfor %}
<h2>Log</h2>
<pre class="log" id="log">loading…</pre>
<script src="{{ url_for('static', filename='app.js') }}"></script>
{% endblock %}
```

```javascript
// app/gui/static/app.js — poll the log tail
(function () {
  var el = document.getElementById("log");
  if (!el) return;
  function refresh() {
    fetch("logs?tail=200").then(function (r) { return r.text(); })
      .then(function (t) { el.textContent = t || "(log empty)"; el.scrollTop = el.scrollHeight; })
      .catch(function () {});
  }
  refresh();
  setInterval(refresh, 5000);
})();
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/gui/ -v`
Expected: PASS (all GUI tests: config_io + runner + app + config routes + status routes).

- [ ] **Step 6: Commit**

```bash
git add app/gui/routes.py app/gui/templates/status.html app/gui/static/app.js tests/gui/test_status_routes.py
git commit -m "feat(gui): run/status/logs screen (run-now, last-run state, polling log tail)"
```

---

### Task 6: Entrypoint process model + waitress server

Run the GUI alongside the scheduler in the one container, gated on `GUI_ENABLED`.

**Files:**
- Create: `app/gui/server.py`
- Modify: `scripts/entrypoint.sh`
- Modify: `tests/bats/entrypoint.bats` (add a `GUI_ENABLED` assertion)

**Interfaces:**
- Consumes: `create_app` (factory), `GUI_PORT`/`GUI_ENABLED` from config/env.
- Produces: `python3 -m app.gui.server` serves the app via waitress on `GUI_PORT`. `entrypoint.sh`: when `GUI_ENABLED != false`, start supercronic in the background and exec the GUI; else exec supercronic (unchanged headless path). `RUN_ONCE` still short-circuits to a single pipeline.

- [ ] **Step 1: Implement `app/gui/server.py`**

```python
# app/gui/server.py — waitress entrypoint: `python3 -m app.gui.server`
from __future__ import annotations
import os
from waitress import serve
from app.gui import create_app

def main() -> None:
    serve(create_app(), host="0.0.0.0", port=int(os.environ.get("GUI_PORT", "8099")))

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing bats test**

Add to `tests/bats/entrypoint.bats` (the fixtures already set config; add `GUI_ENABLED=false` so the test doesn't try to start a server):

```bash
@test "entrypoint with GUI_ENABLED=false emits crontab and does not require the GUI" {
  echo "GUI_ENABLED=false" >>"$CFG/backup.env"
  run bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ "$status" -eq 0 ]
  grep -q "backup-appdata.sh" "$CACHE_DIR/crontab"
}
```

Run: `bats tests/bats/entrypoint.bats` — Expected: the new test FAILS if `GUI_ENABLED` handling breaks `--emit-crontab` (it should still pass the existing tests).

- [ ] **Step 3: Modify `scripts/entrypoint.sh`**

Keep `prepare`, `emit_crontab`, `--emit-crontab`, and `RUN_ONCE` exactly as they are. Change only the final launch block so that after emitting the crontab (and handling `RUN_ONCE`), it chooses GUI vs scheduler-only:

```bash
  # ... after RUN_ONCE handling and emit_crontab ...
  if [ "${GUI_ENABLED:-true}" != "false" ]; then
    log_info "starting scheduler (background) + GUI on port ${GUI_PORT:-8099}"
    supercronic "$CACHE_DIR/crontab" &
    exec python3 -m app.gui.server
  fi
  log_info "GUI disabled; scheduler only"
  exec supercronic "$CACHE_DIR/crontab"
```

(Ensure `GUI_ENABLED` and `GUI_PORT` are exported by `load_config` defaults or read from the environment; add `: "${GUI_ENABLED:=true}"` / `: "${GUI_PORT:=8099}"` defaults in `config.sh` alongside the other defaults if not already present, and add them to its export list.)

- [ ] **Step 4: Run the tests**

Run: `bats tests/bats/entrypoint.bats && shellcheck scripts/entrypoint.sh`
Expected: PASS (existing tests + the new one), shellcheck clean.

- [ ] **Step 5: Commit**

```bash
git add app/gui/server.py scripts/entrypoint.sh scripts/lib/config.sh tests/bats/entrypoint.bats
git commit -m "feat(gui): run GUI (waitress) alongside supercronic; GUI_ENABLED toggle"
```

---

### Task 7: Packaging, CI, README + roadmap

Ship the GUI in the image, test it in CI, and document it (including the deferred roadmap).

**Files:**
- Modify: `Dockerfile` (install flask + waitress; COPY the backup.env.example template)
- Modify: `.github/workflows/ci.yml` (add a `gui` job)
- Modify: `README.md` (GUI section + no-auth warning + manual secrets + Roadmap)

**Interfaces:**
- Consumes: the whole `app/gui` package.
- Produces: `python3 -m app.gui.server` runs in the image; `pip install flask waitress`; README documents usage + roadmap.

- [ ] **Step 1: Install the web deps + template in the Dockerfile**

In the existing `pip install` line (Task-0 baseline installs apprise), extend it to add flask + waitress:

```dockerfile
    && pip install --no-cache-dir --break-system-packages apprise flask waitress
```

Add a COPY for the template the GUI reads (after the `COPY app/` line):

```dockerfile
COPY config/backup.env.example /app/config/backup.env.example
```

- [ ] **Step 2: Add the `gui` CI job**

In `.github/workflows/ci.yml`, alongside the `estimator` job:

```yaml
  gui:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install flask waitress pytest
      - run: python -m pytest tests/gui/ -v
```

- [ ] **Step 3: Document the GUI in `README.md`**

Add a `## GUI` section after the `## Cost estimator` section:

```markdown
## GUI

A small web UI (config editor + run/status/logs) ships in the container, served on `GUI_PORT`
(default 8099). Reach it at `http://<host>:8099`.

> ⚠ **No authentication.** The GUI has no login of its own — put it behind your reverse proxy /
> SSO and never expose it directly to the internet. Set `GUI_ENABLED=false` in `backup.env` to
> disable it and run scheduler-only/headless.

- **Config editor** — edits `backup.env` (regenerated from the bundled `backup.env.example`
  template) and `includes-media.txt`. Secret fields (AWS keys, restic password) are **write-only**:
  they never display existing values; leave a field blank to keep it, fill it to overwrite.
- **Run & status** — trigger an appdata/media backup now, see the last-run outcome per pipeline,
  and watch the live log tail.

Prefer to manage secrets by hand? Create the file directly instead of using the form:

    cp config/secrets.env.example /config/secrets.env
    chmod 600 /config/secrets.env
    $EDITOR /config/secrets.env   # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, RESTIC_PASSWORD

## Roadmap

Planned, not yet built:

- **Restore wizard** — guided both-tier restore in the GUI (incl. the Glacier/Deep Archive thaw flow).
- **Media-dir picker** — browse the media mount to build `includes-media.txt`.
- **Provisioning wizard** — the three-mode AWS setup in the GUI.
- **Cost-estimator screen** — interactive what-if over the `estimate` module.
- **OIDC authentication** — native OpenID Connect login, so the GUI can stand on its own without an external proxy.
- **Per-run history** — a persisted run history beyond the last-run state.
```

Also add to the **Development** section:

```markdown
- `python3 -m pytest tests/gui/` — GUI unit tests (Flask test client).
```

- [ ] **Step 4: Verify what can be verified without Docker**

Run: `python3 -m pytest tests/gui/ -v` (all pass) and `python3 -c "import app.gui.server"` (imports; requires `pip install flask waitress` locally). Statically confirm the Dockerfile `pip` + `COPY` edits and the CI YAML. Note in the commit that the image build + in-container GUI start are CI-verified only (no Docker on the dev host).

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .github/workflows/ci.yml README.md
git commit -m "feat(gui): package into image (flask+waitress), CI job, README + roadmap"
```

---

## Self-Review

**Spec coverage:**
- §1 two screens, shells out to engine → Tasks 4, 5; Global Constraints. ✅
- §3 resolved decisions (Flask, waitress, write-only secrets, no auth, process model, example-as-template) → Tasks 1, 3, 6; constraints. ✅
- §4 units (`config_io` sole file I/O; `runner` sole subprocess/cache; routes/templates; factory; `security`) → Tasks 1, 2, 3. ✅
- §5 config editor (example-template backup.env, includes textarea, write-only secrets, manual instructions, light validation) → Tasks 1, 4. ✅ (Light Python-side field validation beyond required-key rendering is minimal in Task 4; deeper validation stays with the engine per spec — noted, not a gap.)
- §6 run/status/logs (run-now subprocess, state JSON, log-tail polling) → Tasks 2, 5. ✅
- §7 security (no-auth banner, write-only secrets, CSRF, cookie flags) → Tasks 3, 4, 5. ✅
- §8 entrypoint process model (supercronic bg + waitress fg, GUI_ENABLED, RUN_ONCE preserved) → Task 6. ✅
- §9 packaging/CI/docs (Dockerfile deps + template COPY, gui CI job, README + roadmap) → Task 7. ✅
- §10 testing (config_io round-trip incl. template comments, secrets write-only, runner, routes, CSRF-reject, secrets-never-rendered) → Tasks 1–5. ✅
- §11 roadmap incl. OIDC + restore wizard → Task 7 README. ✅

**Placeholder scan:** Every code step has real code. The Task-3 index/status/config stubs are explicitly labeled temporary and are replaced in Tasks 4–5 (called out in-line), not left as "TODO". No "add validation"/"handle errors" hand-waves.

**Type consistency:** `config_io` function names/signatures (Task 1) match all call sites in routes (Task 4). `runner.PIPELINES`/`read_state`/`tail_log`/`trigger_backup` (Task 2) match routes (Task 5) and tests. `create_app` config keys (`CONFIG_DIR`, `CACHE_DIR`, `SCRIPTS_DIR`, `TEMPLATE_PATH`, `SECRET_KEY`) are consistent across Tasks 3–6 and the conftest. `security.issue_csrf`/`verify_csrf` consistent between Task 3 and Tasks 4–5. Blueprint endpoint names (`gui.config_page`, `gui.config_save`, `gui.status_page`, `gui.run`, `gui.logs`, `gui.index`) are consistent across routes, templates, and tests.
