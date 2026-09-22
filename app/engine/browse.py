"""Pure tree-folding for the data explorer. No I/O except level_from_restic_ls
reading a cache file. Turns a flat list of path records into one directory level."""
from __future__ import annotations
import json, sys

def _norm(p: str) -> str:
    return p.strip("/")

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
        elif rest:
            if r.get("type") == "dir":        # an (possibly empty) directory AT this level
                dirs.setdefault(rest, True)
            else:                             # a file directly at this level
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
            if not isinstance(o, dict):
                continue
            if o.get("struct_type") == "node" or ("path" in o and "message_type" not in o and o.get("struct_type") != "snapshot"):
                if "path" in o:
                    records.append({"path": o["path"], "type": o.get("type"), "size": o.get("size")})
    return _entries(fold_level(records, cur_dir), cur_dir)

def level_from_rclone_lsjson(json_text: str, cur_dir: str) -> dict:
    """One browse level from an `rclone lsjson` array (already one directory
    deep -- no folding needed, just map + sort). Tolerates empty/blank stdin
    and any non-array/malformed JSON by returning an empty level."""
    cur = _norm(cur_dir)
    text = (json_text or "").strip()
    items = []
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = []
        if isinstance(parsed, list):
            items = parsed
    dirs = []
    files = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("Name")
        if not name:
            continue
        if it.get("IsDir"):
            dirs.append({"name": name, "kind": "dir", "size": None,
                         "storage_class": None, "modified": None})
        else:
            files.append({"name": name, "kind": "file", "size": it.get("Size"),
                          "storage_class": it.get("Tier") or None, "modified": it.get("ModTime")})
    dirs.sort(key=lambda e: e["name"])
    files.sort(key=lambda e: e["name"])
    return {"path": cur, "entries": dirs + files}

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--rclone":
        # python3 -m app.engine.browse --rclone <cur_dir>  (reads rclone lsjson from stdin)
        # Compact separators (no spaces) to match the bash-side grep/string checks
        # that scan this CLI's stdout for shape (e.g. restore.sh's browse arm callers).
        _cur = sys.argv[2] if len(sys.argv) > 2 else ""
        print(json.dumps(level_from_rclone_lsjson(sys.stdin.read(), _cur), separators=(",", ":")))
    else:                                        # python3 -m app.engine.browse <ls_json> <cur_dir>
        print(json.dumps(level_from_restic_ls(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")))
