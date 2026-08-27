# app/estimator/cli.py — the ONLY reader of env/backup.env and writer of stdout.
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from .model import PipelineInputs, Scenario, Estimate, estimate, STORAGE_CLASSES
from .prices import load_prices

_ASSUMPTIONS = """cost model assumptions (decision-support, not billing-accurate):
  * 128 KB minimum object size applied as max(size, object_count * 128KB), not per-object
  * data-transfer-out uses a single flat first-tier $/GB rate
  * rotation charges churned bytes the full minimum-storage-duration remainder
  * one bundled price table (us-east-1); every figure is stamped with its capture date"""

def _read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.split("#", 1)[0].strip().strip('"').strip("'")
    return env

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="estimate", description="Estimate S3 backup costs.")
    ap.add_argument("--config-dir", default="/config",
                    help="directory holding backup.env (region + storage classes fallback)")
    ap.add_argument("--json", action="store_true", help="emit structured JSON instead of a table")
    ap.add_argument("--assumptions", action="store_true", help="print modeling assumptions and exit")
    ap.add_argument("--region")
    ap.add_argument("--retrieval-tier", default=None)
    ap.add_argument("--restore-fraction", type=float, default=None)
    ap.add_argument("--restores-per-year", type=float, default=None)
    ap.add_argument("--versioning-retention-days", type=int, default=None)
    for p in ("appdata", "media"):
        ap.add_argument(f"--{p}-size-gb", type=float, default=None)
        ap.add_argument(f"--{p}-file-count", type=int, default=None)
        ap.add_argument(f"--{p}-storage-class", default=None)
        ap.add_argument(f"--{p}-backups-per-month", type=float, default=None)
        ap.add_argument(f"--{p}-change-rate-pct", type=float, default=None)
    ap.add_argument("--media-packing", action="store_true")
    ap.add_argument("--media-pack-member-gb", type=float, default=None)
    return ap

def _pipeline(args, env, name: str, default: PipelineInputs) -> PipelineInputs:
    g = lambda attr, fallback: getattr(args, f"{name}_{attr}") if getattr(args, f"{name}_{attr}") is not None else fallback
    cls = g("storage_class", env.get(f"{name.upper()}_STORAGE_CLASS", default.storage_class))
    if cls not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class '{cls}' (choose from {', '.join(STORAGE_CLASSES)})")
    return PipelineInputs(
        size_gb=g("size_gb", default.size_gb),
        file_count=g("file_count", default.file_count),
        storage_class=cls,
        packing=(args.media_packing if name == "media" else False),
        pack_member_gb=(args.media_pack_member_gb if name == "media" and args.media_pack_member_gb is not None else default.pack_member_gb),
        backups_per_month=g("backups_per_month", default.backups_per_month),
        change_rate_pct=g("change_rate_pct", default.change_rate_pct),
    )

def build_scenario(args, env: dict[str, str]) -> Scenario:
    d = Scenario()  # defaults
    region = args.region if args.region is not None else env.get("AWS_REGION", d.region)
    return Scenario(
        region=region,
        appdata=_pipeline(args, env, "appdata", d.appdata),
        media=_pipeline(args, env, "media", d.media),
        versioning_retention_days=(
            args.versioning_retention_days if args.versioning_retention_days is not None
            else d.versioning_retention_days),
        restore_fraction=args.restore_fraction if args.restore_fraction is not None else d.restore_fraction,
        restores_per_year=args.restores_per_year if args.restores_per_year is not None else d.restores_per_year,
        retrieval_tier=args.retrieval_tier if args.retrieval_tier is not None else d.retrieval_tier,
    )

def estimate_to_dict(est: Estimate) -> dict:
    return asdict(est)

def render_table(est: Estimate) -> str:
    lines = [f"S3 backup cost estimate  (prices: {est.region} @ {est.price_date} — {est.price_source})", ""]
    for name, li in est.pipelines.items():
        lines += [
            f"[{name}]  billed {li.billed_gb:.2f} GB across {li.effective_object_count} objects",
            f"  storage/mo        ${li.storage:,.2f}",
            f"  versioning/mo     ${li.versioning:,.2f}",
            f"  ingest/mo         ${li.ingest_monthly:,.2f}",
            f"  rotation/mo       ${li.rotation_monthly:,.2f}",
            f"  upfront (once)    ${li.upfront_onetime:,.2f}",
            f"  restore/event     ${li.restore_per_event:,.2f}",
            "",
        ]
    lines += [
        f"MONTHLY total       ${est.monthly_total:,.2f}",
        f"FIRST-YEAR total    ${est.first_year_total:,.2f}",
        f"FULL-RESTORE (once) ${est.full_restore_total:,.2f}",
    ]
    return "\n".join(lines)

def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.assumptions:
        print(_ASSUMPTIONS)
        return 0
    env: dict[str, str] = {}
    cfg = Path(args.config_dir) / "backup.env"
    if cfg.is_file():
        env = _read_env_file(cfg)
    try:
        scenario = build_scenario(args, env)
        est = estimate(scenario, load_prices(scenario.region))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(estimate_to_dict(est), indent=2) if args.json else render_table(est))
    return 0
