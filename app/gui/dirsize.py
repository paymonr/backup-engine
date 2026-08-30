# app/gui/dirsize.py — total bytes + file count of a source folder, confined to
# SOURCE_ROOT. Used to seed a NEW job's size in the wizard (no bucket data yet).
from __future__ import annotations
import os
from . import fsbrowse

def dir_size(source_root: str, rel_path: str) -> dict:
    root = fsbrowse.safe_resolve(source_root, rel_path)  # raises PathError on escape
    total = 0
    count = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.stat(os.path.join(dirpath, f)).st_size
                count += 1
            except OSError:
                continue
    return {"bytes": total, "count": count}
