# Task 9: Job-delete UI states the bucket is kept

**Status:** COMPLETE

**Commit:** 46f01a2

**Files Modified:**
- `app/gui/templates/job.html` - Added message to delete control
- `tests/gui/test_job_page_routes.py` - Added unit test for template conditional

## Changes Made

### Template (job.html)
Added a conditional note below the delete form that renders when a job has `dedicated=True` and a `bucket` field:
```html
{% if job.dedicated and job.bucket %}
<p class="hint" style="margin-top:var(--sp-2)">Deleting this job won't delete its bucket <code>{{ job.bucket }}</code> or its data (it stays in S3 and keeps billing).</p>
{% endif %}
```

This message appears in the `···` menu's delete control area, positioned after the delete form, alerting users that their dedicated bucket will remain intact if they delete the job.

### Test (test_job_page_routes.py)
Two real integration tests that render through the actual Flask route:

1. **test_job_page_delete_notes_bucket_kept_for_dedicated** - Positive test:
   - Seeds a dedicated job via `jobs_io.upsert()` with `dedicated=True, bucket="bw-backups-photos"`
   - Seeds run history via `_seed_30_ok()`
   - Calls `client.get("/jobs/photos")` to render the real route
   - Asserts the message and bucket name appear in the rendered HTML

2. **test_job_page_delete_does_not_note_bucket_for_non_dedicated** - Negative test:
   - Uses the existing non-dedicated `appdata` job
   - Verifies the message does NOT appear for non-dedicated jobs

## Test Results
All 20 tests pass:
- Existing 18 tests remain green
- 2 new integration tests verify positive and negative cases through real route rendering

## TDD Compliance
1. Test written first (failed) ✓
2. Feature implemented ✓
3. Test now passes ✓
4. Commit created with brief's message ✓

---

## Fix Round 1
Replaced standalone Jinja2.Template unit test with proper integration tests that render through the actual Flask route (`client.get("/jobs/<name>")` via real route). This ensures:
- Template is rendered correctly through the real route context
- `job` object is passed properly from the route to the template
- Message appears/suppresses correctly based on actual job data
- Regression protection if template or route logic changes

Test results: 20 passed (18 existing + 2 new integration tests)

## Concerns
None. The feature is simple, well-scoped, and tested with proper integration tests. The message is clear and positioned appropriately in the UI hierarchy.
