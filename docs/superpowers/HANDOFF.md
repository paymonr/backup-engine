# HANDOFF — UI redesign ("Night Shift") SDD execution

**Purpose:** let a fresh Claude session (including a different Claude account) resume this
in-progress implementation if the current session hits a usage limit. Read this top to bottom,
then follow **How to resume**.

_Last updated: 2026-09-16. Keep the "Current state" block below current at every task boundary._

---

## Current state

- **Branch:** `ui-redesign-nightshift` (pushed to `origin` — GitHub `paymonr/backup-engine`).
  Stacked ~119 commits above `master`; sits on `impeccable-versioned-files`. Do NOT merge to
  master or push over the open PRs #8/#9/#10 — this branch is its own tower.
- **Plan:** `docs/superpowers/plans/2026-09-15-ui-redesign.md` — 15 tasks, engine-first.
- **Spec (the authority):** `docs/superpowers/specs/2026-09-15-ui-redesign-design.md` (3760 lines,
  self-contained; Appendix B holds rulings R1–R11). The plan argues from the spec; conflicts
  resolve to the spec.
- **Done:** Tasks **1–6 (the whole engine layer)** complete and review-clean. Task **7a**
  (design-system `style.css` rewrite) committed at `7ad3958` — under task review as of this write.
- **Next:** finish 7a's review → Task **7b** (shell: base.html/nav/error pages/flash/route
  renames) → screens **8–14** → polish **15**.
- **Suite:** green at **710 passed, 1 deselected**. Green gate command:
  `python3 -m pytest -q --deselect tests/estimator/test_billing.py::test_forecast_parses`
  (the deselected test is date-sensitive and unrelated). Plus bats where a task adds them.

## How to resume

1. `git fetch origin && git checkout ui-redesign-nightshift && git pull --ff-only` — get the code.
   (Same-machine handoff: the working copy at `/home/paymon/src/backup-engine` already has it.)
2. Confirm the green gate passes (command above).
3. **Invoke the skill** `superpowers:subagent-driven-development` and follow it. It IS the process
   that produced every commit here. Do not free-hand the implementation.
4. **Read the SDD ledger** — the task-level source of truth:
   `.superpowers/sdd/2026-09-15-ui-redesign/progress.md`
   - It is gitignored scratch but lives on THIS machine's disk (reachable for a same-machine,
     different-login handoff). If a fresh checkout lacks it, reconstruct state from `git log` +
     this file's "Current state" block.
   - A line `Task N: complete (...)` means that task is DONE — never re-dispatch it. Resume at the
     first task with no `complete` line. A task whose last line is a fix round is mid-loop: resume
     that loop at the next round.
   - The ledger also holds every **Ruling** made so far (R-A … R-G and per-task rulings) and the
     per-task **carry-forwards** — read them before dispatching the next task; some bind later tasks
     (e.g. Task 11 must wire `RESTORE_ROOT`/`RESTORE_ROOT_HOST` into `app.config`; Task 12 wires the
     `board_cost`/`restore_quote` stubs).
5. Trust the ledger and `git log` over any recollection.

## Model / budget policy (owner is usage-budget sensitive — see memory [[mind-the-usage-budget]])

- Implementers on **Opus** (owner's choice); pure-transcription tasks may drop to Sonnet.
- Reviewers **scaled to the diff**: Sonnet for small/mechanical, Opus for delicate logic.
- Final whole-branch review: **most capable** available model.
- Always pass the model explicitly when dispatching a subagent.

## Where the rest of the durable state lives

- **Git commits** — the record; now on `origin`.
- **SDD ledger** — `.superpowers/sdd/2026-09-15-ui-redesign/progress.md` (on-disk, machine-local).
- **Auto-memory** — `~/.claude/projects/-home-paymon-src-backup-engine/memory/` (see `MEMORY.md`
  index and `ui-redesign.md`; machine/user-local).

## Global constraints (do not violate — full list in the plan's "Global Constraints")

- Estimator math is **frozen**: never edit `app/estimator/model.py|tiered.py|prices.py|usage.py|
  billing.py|schedule.py` or `tests/estimator/*` — a Task-1 SHA-256 guard fails the suite if you do.
  Extend cost behaviour only via adapters in `app/gui/estimate_io.py`.
- One vocabulary (spec §4.3/§4.4), enforced by `tests/gui/test_vocabulary.py`.
- Persisted contracts (`jobs.json`, state files, `runs.jsonl`) only **grow**.
- TDD, DRY, YAGNI, **one commit per task** (frequent commits — reboot-safe, see memory
  [[commit-often-reboot-safe]]).

## Deploy

Not deployed mid-branch. The owner runs the gated deploy to the Unraid box themselves (see memory
`unraid-deploy-procedure`). Do not deploy from an implementer session.
