# app/gui/dirsize.py — total bytes + file count of a source folder, confined to
# SOURCE_ROOT. Uses native `du`/`find` (C-native, whole-tree) rather than a Python
# os.walk, so even a multi-TB media library returns a REAL total instead of an
# 8-second partial guess. Fired async by the wizard, so it never blocks the live
# estimate. Confinement (fsbrowse.safe_resolve) runs FIRST; the resolved absolute
# path is passed to the tools as argv (no shell) -> injection-safe. `du`/`find` do
# not follow symlinks out of the tree by default (matches the old followlinks=False).
from __future__ import annotations
import os
import subprocess
from . import fsbrowse

# Generous ceiling: `du` is ~10-50x faster than a Python walk, so this comfortably
# covers very large trees. Size (the number the user decides on) uses the full
# budget; file count is best-effort within a shorter slice.
_TIMEOUT_S = 180.0


def dir_size(source_root: str, rel_path: str, *, timeout_s: float = _TIMEOUT_S) -> dict:
    root = fsbrowse.safe_resolve(source_root, rel_path)  # confinement FIRST (raises PathError)
    root_s = str(root)
    # A confined-but-missing path is simply an empty tree (not a measurement
    # failure): return the empty result cleanly rather than letting `du` error.
    if not os.path.isdir(root_s):
        return {"bytes": 0, "count": 0}
    total = 0
    count = 0
    capped = False

    # SIZE — GNU coreutils `du -sb`: apparent bytes of the whole tree, one native
    # pass. This is the number the user makes decisions on, so it gets the full
    # budget. Only a genuine failure/timeout flags "capped".
    try:
        du = subprocess.run(["du", "-sb", root_s], capture_output=True, text=True,
                            timeout=timeout_s)
        field = du.stdout.split("\t", 1)[0].strip() if du.stdout else ""
        if du.returncode == 0 and field.isdigit():
            total = int(field)
        else:
            capped = True
    except subprocess.TimeoutExpired:
        capped = True
    except (OSError, ValueError):
        capped = True

    # COUNT — `find -type f | wc -l` (busybox find + coreutils wc). Cosmetic (only
    # the readout uses it), so it's best-effort within a shorter slice; a failure
    # just leaves count at 0 and the readout omits the file tally.
    find = wc = None
    try:
        find = subprocess.Popen(["find", root_s, "-type", "f"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        wc = subprocess.Popen(["wc", "-l"], stdin=find.stdout,
                              stdout=subprocess.PIPE, text=True)
        find.stdout.close()
        out, _ = wc.communicate(timeout=min(timeout_s, 60.0))
        find.wait(timeout=5)
        count = int(out.strip() or "0")
    except Exception:
        for p in (find, wc):
            try:
                if p is not None:
                    p.kill()
            except Exception:
                pass

    result = {"bytes": total, "count": count}
    if capped:
        result["capped"] = True
    return result
