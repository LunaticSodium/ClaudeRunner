# claude-runner Self-Test Report

**Date/Time:** 2026-03-16T09:05:00Z
**Runner:** claude-sonnet-4-6
**Branch:** marathon

---

## Test Results

| Run | Passed | Failed | Skipped | Total |
|-----|--------|--------|---------|-------|
| Initial | 258 | 7 | 0 | 265 |
| Final | 265 | 0 | 0 | 265 |

---

## Failures Found and Fixed

### 1. `TestGitWorkflow` — 5 tests (AttributeError: `_book_path`, `_project_id`)

**Root cause:** `_make_runner()` in `tests/test_milestone_and_git.py` constructs a `TaskRunner` via `__new__` and manually sets attributes. It was missing two instance attributes (`_book_path`, `_project_id`) that were added to `TaskRunner.__init__` after the tests were written.

**Fix:** Added the missing attributes to the test helper:
- `runner._book_path = None`
- `runner._project_id = "test-task"`

**File changed:** `tests/test_milestone_and_git.py`

---

### 2. `TestWorkingDirValidation.test_nonexistent_dir_is_created` — 1 test

**Root cause:** The `SandboxConfig.working_dir_must_be_dir` validator in `claude_runner/project.py` checked for existence and raised on non-directory paths, but did not create the directory when it didn't exist. The test expected auto-creation.

**Fix:** Added `v.mkdir(parents=True, exist_ok=True)` when the path does not exist.

**File changed:** `claude_runner/project.py`

---

### 3. `TestRateLimitDetectorCallback.test_fallback_reset_time_when_no_timestamp` — 1 test

**Root cause:** The test asserted the fallback reset time was approximately 1 hour from now (`3500 < delta < 3700`). The implementation uses `_FALLBACK_WAIT_SECONDS = 18_000` (5 hours), matching Anthropic's actual usage-limit reset window. The test was stale.

**Fix:** Updated the assertion to match the documented 5-hour fallback: `17900 < delta < 18100`.

**File changed:** `tests/test_rate_limit.py`

---

## Overall Status

**PASS** — 265/265 tests passing.
