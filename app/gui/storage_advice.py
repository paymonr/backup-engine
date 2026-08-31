# app/gui/storage_advice.py — PURE guidance derived from the loaded PriceTable.
# No I/O, no Flask, no cost math beyond formatting existing PriceTable fields and
# calling the model's own restore/schedule helpers via the caller. model.py is not
# imported here except for the class ORDER constant.
from __future__ import annotations
from ..estimator.prices import PriceTable
from ..estimator.model import STORAGE_CLASSES
from ..estimator.schedule import backups_per_month

COLD_CLASSES = ("GLACIER", "DEEP_ARCHIVE")  # thaw-required to READ

# Read latency is a physical property of the class, not in the price table.
READ_ACCESS = {
    "STANDARD": "instant",
    "STANDARD_IA": "instant",
    "GLACIER_IR": "instant (ms)",
    "GLACIER": "thaw required (mins–hrs)",
    "DEEP_ARCHIVE": "thaw required (hrs)",
}
USE_CASE = {
    "STANDARD": "Hot data you read often; no retrieval fee.",
    "STANDARD_IA": "Infrequent access, still instant; 30-day minimum.",
    "GLACIER_IR": "Archive you might need instantly; 90-day minimum.",
    "GLACIER": "Cheap archive you rarely restore; thaw to read.",
    "DEEP_ARCHIVE": "Cheapest storage; slow, pricier restore; 180-day minimum.",
}


def _retrieval_str(prices: PriceTable, cls: str) -> str:
    table = prices.retrieval_per_gb.get(cls)
    if not table:
        return "none"
    lo = min(table.values())
    return "${:.4f}/GB".format(lo) if lo else "$0.00/GB (Bulk)"


def storage_class_info(prices: PriceTable) -> list[dict]:
    rows = []
    for cls in STORAGE_CLASSES:
        rows.append({
            "name": cls,
            "rate_gb_month": prices.storage_gb_month.get(cls, 0.0),
            "read_access": READ_ACCESS.get(cls, "instant"),
            "min_duration_days": int(prices.min_storage_duration_days.get(cls, 0)),
            "retrieval": _retrieval_str(prices, cls),
            "egress_per_gb": prices.data_transfer_out_per_gb,
            "cold": cls in COLD_CLASSES,
            "use_case": USE_CASE.get(cls, ""),
        })
    return rows


def _warmer_than(a: str, b: str) -> bool:
    """True if class `a` is WARMER (earlier in STORAGE_CLASSES) than `b`."""
    return STORAGE_CLASSES.index(a) < STORAGE_CLASSES.index(b)


def class_advice(job_type: str, storage_class: str, schedule: str,
                 saved_class, prices: PriceTable) -> list[dict]:
    out: list[dict] = []
    min_days = int(prices.min_storage_duration_days.get(storage_class, 0))

    # 1. restic (Snapshots) cannot operate on a thaw-required class (spec §8-2):
    #    strong WARNING that steers to Versioned files; NOT a hard block.
    if job_type == "versioned" and storage_class in COLD_CLASSES:
        out.append({"level": "danger", "text": (
            "Snapshots use restic, which reads its repository on every run — it "
            "cannot operate on a thaw-required class. Pick an instant class "
            "(STANDARD, STANDARD_IA, GLACIER_IR) or switch this to Versioned "
            "files, which stores cold natively.")})

    # 1b. restic re-reads its repository on EVERY run. An instant class that still
    #     bills a per-GB retrieval fee (STANDARD_IA / GLACIER_IR) therefore charges
    #     retrieval on every run — often more than the cheaper storage saves (spec
    #     §2). STANDARD has no retrieval fee; cold classes are the danger above.
    #     Data-driven: instant (not cold) AND a non-empty retrieval table.
    elif job_type == "versioned" and prices.retrieval_per_gb.get(storage_class):
        out.append({"level": "warn", "text": (
            "Snapshots use restic, which re-reads its repository every run. {} bills "
            "a per-GB retrieval fee on each read, so every run incurs retrieval "
            "charges — often more than the cheaper storage saves. STANDARD has no "
            "retrieval fee; use it for snapshots, or use Archive / Versioned files "
            "for a cheaper class.").format(storage_class)})

    # 2. Minimum-duration / early-deletion note (any class with a minimum).
    if min_days:
        out.append({"level": "info", "text": (
            "{} bills a {}-day minimum. Delete, replace, or transition a file "
            "sooner and you still pay storage through day {}.").format(
                storage_class, min_days, min_days)})

    # 3. High-churn nudge: replacing files often on a long-minimum class re-incurs
    #    the minimum each time; a shorter-minimum class can be cheaper (spec §5).
    if job_type in ("versioned-files", "archive") and min_days >= 180:
        if backups_per_month(schedule) >= 4:
            out.append({"level": "warn", "text": (
                "You back up often on a {}-day-minimum class. Each replaced file "
                "re-incurs that minimum, so a shorter-minimum class (STANDARD_IA "
                "30d or GLACIER_IR 90d) can be cheaper despite a higher per-GB "
                "rate.").format(min_days)})

    # 4. Edit transition explainer (only when the class actually changed).
    if saved_class and saved_class != storage_class:
        out.append({"level": "info", "text": (
            "Changing the class affects FUTURE uploads only — files already stored "
            "stay in {}. Moving existing data is a separate admin action.").format(
                saved_class)})
        if _warmer_than(storage_class, saved_class):
            out.append({"level": "warn", "text": (
                "This is a warm-up change ({} → {}): existing objects can't "
                "lifecycle-transition to a warmer class — they need a restore "
                "(thaw) + copy, which costs retrieval + requests.").format(
                    saved_class, storage_class)})

    return out
