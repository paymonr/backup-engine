# tests/conftest.py — repo-wide test safety net (shared by tests/estimator, tests/gui and
# tests/engine; pytest.ini's testpaths).
import pytest


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """post-wave review, minor 3: no test may fire a real Apprise notification or run the real
    `apprise` binary. Deletes APPRISE_URLS from the environment (so a test never inherits one
    from the container/host, e.g. inside a deploy container) and stubs
    app.engine.lifecycle._send_notification to a no-op. A test that asserts notification
    behavior opts back in explicitly by re-patching _send_notification and/or setting
    APPRISE_URLS itself (see tests/engine/test_lifecycle_final_wave.py's `sent` fixture and
    the tests around it) -- that local patch simply overrides this one for its own duration."""
    monkeypatch.delenv("APPRISE_URLS", raising=False)
    from app.engine import lifecycle
    monkeypatch.setattr(lifecycle, "_send_notification", lambda urls, title, body: False)
