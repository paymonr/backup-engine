# app/estimator/cli.py — the ONLY writer of stdout for the estimator. Reads the
# configured jobs (config/jobs.json) via the GUI adapter and prices them per job.
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import asdict, replace
from .model import Scenario, Estimate, estimate
from .prices import load_prices
from ..gui import estimate_io

_ASSUMPTIONS = """cost model assumptions (decision-support, not billing-accurate):
  * 128 KB minimum object size applied as max(size, object_count * 128KB), not per-object
  * data-transfer-out uses a single flat first-tier $/GB rate
  * rotation charges churned bytes for the full minimum-storage-duration as a
    conservative early-deletion proxy (intentionally overlaps with versioning,
    a deliberate conservative upper bound)
  * one bundled price table (us-east-1); every figure is stamped with its capture date"""

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="estimate", description="Estimate S3 backup costs.")
    ap.add_argument("--config-dir", default="/config",
                    help="directory holding jobs.json + backup.env (region fallback)")
    ap.add_argument("--source-root", default="/backup/media",
                    help="root the jobs' sources live under (reserved for on-disk sizing)")
    ap.add_argument("--json", action="store_true", help="emit structured JSON instead of a table")
    ap.add_argument("--assumptions", action="store_true", help="print modeling assumptions and exit")
    ap.add_argument("--region", default=None)
    ap.add_argument("--retrieval-tier", default=None)
    ap.add_argument("--restore-fraction", type=float, default=None)
    ap.add_argument("--restores-per-year", type=float, default=None)
    ap.add_argument("--versioning-retention-days", type=int, default=None)
    return ap

def build_scenario(args) -> Scenario:
    """Build the N-job Scenario from config/jobs.json, then overlay any global
    what-if flags. Per-job sizing/overrides live in the GUI; the CLI tweaks the
    scenario-level globals only."""
    base = estimate_io.scenario_from_jobs(args.config_dir, args.source_root)
    changes: dict = {}
    if args.region is not None:
        changes["region"] = args.region
    if args.retrieval_tier is not None:
        changes["retrieval_tier"] = args.retrieval_tier
    if args.restore_fraction is not None:
        changes["restore_fraction"] = args.restore_fraction
    if args.restores_per_year is not None:
        changes["restores_per_year"] = args.restores_per_year
    if args.versioning_retention_days is not None:
        changes["versioning_retention_days"] = args.versioning_retention_days
    return replace(base, **changes) if changes else base

def estimate_to_dict(est: Estimate) -> dict:
    return asdict(est)

def render_table(est: Estimate) -> str:
    lines = [f"S3 backup cost estimate  (prices: {est.region} @ {est.price_date} — {est.price_source})", ""]
    if not est.jobs:
        lines.append("(no jobs configured — add a job to see an estimate)")
        lines.append("")
    for name, li in est.jobs.items():
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
    try:
        scenario = build_scenario(args)
        prices = load_prices(scenario.region)
        if scenario.retrieval_tier not in prices.retrieval_request_per_1k:
            raise ValueError(f"unknown retrieval tier '{scenario.retrieval_tier}'")
        est = estimate(scenario, prices)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(estimate_to_dict(est), indent=2) if args.json else render_table(est))
    return 0
