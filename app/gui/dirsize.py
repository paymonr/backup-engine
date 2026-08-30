# app/gui/dirsize.py — total bytes + file count of a source folder, confined to
# SOURCE_ROOT. Used to seed a NEW job's size in the wizard (no bucket data yet).
# Bounded by a wall-clock budget so /jobs/source-size can't hang on a huge share:
# past the budget it returns what it has so far with "capped": True. The estimate
# path never calls this (see estimate_io.wizard_estimate) — it must stay instant.
from __future__ import annotations
import os
import time
from . import fsbrowse

# Wall-clock ceiling for a single walk. /jobs/source-size is fired async and is a
# best-effort seed, so a giant tree is bounded rather than allowed to hang.
_BUDGET_S = 8.0

def dir_size(source_root: str, rel_path: str, *, budget_s: float = _BUDGET_S) -> dict:
    root = fsbrowse.safe_resolve(source_root, rel_path)  # raises PathError on escape (confinement FIRST)
    total = 0
    count = 0
    capped = False
    start = time.monotonic()
    try:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                try:
                    total += os.stat(os.path.join(dirpath, f)).st_size
                    count += 1
                except OSError:
                    continue  # unreadable/vanished file — skip, don't abort the walk
            if time.monotonic() - start >= budget_s:
                capped = True  # over budget: stop and return the partial total
                break
    except OSError:
        # A failure enumerating the tree (permissions, races) degrades to what we
        # have rather than raising — the size is only a hint, never a hard dependency.
        capped = True
    out = {"bytes": total, "count": count}
    if capped:
        out["capped"] = True
    return out
