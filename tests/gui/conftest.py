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
