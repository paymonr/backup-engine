# tests/packaging/test_volumes_mapped.py — every VOLUME the image declares must be mapped by the
# Unraid template and the compose file. An unmapped VOLUME makes Docker attach an anonymous volume,
# which the Appdata Backup plugin can't resolve ("'<id>' does NOT exist! Please check your
# mappings!" — one Alert per container per run).
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def declared_volumes() -> set[str]:
    text = (ROOT / "Dockerfile").read_text()
    found: set[str] = set()
    for m in re.finditer(r"^VOLUME\s+(.+)$", text, re.M):
        arg = m.group(1).strip()
        found.update(json.loads(arg) if arg.startswith("[") else arg.split())
    return found


def template_targets() -> set[str]:
    tree = ET.parse(ROOT / "backup-engine.xml")
    return {c.get("Target") for c in tree.iter("Config") if c.get("Type") == "Path"}


def compose_destinations() -> set[str]:
    # Short-syntax mounts only ("- src:/dest[:mode]"); port lines never have an absolute destination.
    text = (ROOT / "docker-compose.yml").read_text()
    return set(re.findall(r"^\s*-\s*[^\s#:]+:(/[^\s:#]+)", text, re.M))


def test_dockerfile_declares_volumes():
    assert declared_volumes(), "parser found no VOLUME line — the test would pass vacuously"


def test_every_declared_volume_is_mapped_by_the_unraid_template():
    assert declared_volumes() - template_targets() == set()


def test_every_declared_volume_is_mapped_by_docker_compose():
    assert declared_volumes() - compose_destinations() == set()
