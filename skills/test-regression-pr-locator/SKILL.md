---
name: test-regression-pr-locator
description: >
  When a test case passes at time A and fails at time B, automatically analyzes PRs merged near that interval,
  plus optionally older PRs that may have caused a delayed regression. Uses multi‑dimensional weighted scoring
  (file paths, exact stack match, semantics, dependency changes, time proximity, and latent‑bug heuristics).
  Provides detailed reasoning, fix suggestions, and an executable git bisect command.
  Designed for rapid root‑cause localization of sudden or delayed test failures in CI pipelines.
---

# Test Regression PR Locator (Enhanced with Delayed‑Failure Detection)

## Trigger Conditions

- User explicitly provides a test case name, the last known pass time, and the first observed failure time.
- Or user asks "When did this test start failing?" or "Which PR caused this?" with sufficient context.
- If information is incomplete (missing time, test case, or repo URL), proactively ask for missing items and attempt to
  auto‑extract times from CI logs.

## Input Parameters

| Parameter              | Type     | Required | Description                                                                                  |
|------------------------|----------|----------|----------------------------------------------------------------------------------------------|
| `repo_url`             | string   | yes      | Full repository URL (GitHub/GitLab), HTTPS or SSH                                            |
| `test_case`            | string   | yes      | Test case name, identifier, or path (e.g., `TestLogin`, `test/user_test.py::test_login`)     |
| `pass_time`            | datetime | yes      | Last confirmed pass time (ISO 8601, e.g., `2025-03-01T10:00:00Z`)                            |
| `fail_time`            | datetime | yes      | First observed failure time (ISO 8601)                                                       |
| `branch`               | string   | no       | Target branch, default `main` or `master`                                                    |
| `test_file_hint`       | string   | no       | Test file path (to improve matching precision)                                               |
| `failure_log`          | string   | no       | Full failure log (supports multi-line, used for exact stack matching)                        |
| `max_pr_count`         | integer  | no       | Maximum number of PRs to analyze per window (default 200)                                    |
| `enable_auto_verify`   | boolean  | no       | Whether to attempt automatic verification of suspicious PRs (default false)                  |
| `historic_window_days` | integer  | no       | Days before `pass_time` to search for older PRs if no immediate suspect is found (default 7) |

## Execution Flow

### Step 0: Pre‑validation & Info Completion

- Ensure `pass_time < fail_time`; otherwise swap automatically and notify the user.
- Check interval length: if longer than 7 days, suggest narrowing down (e.g., via binary search) to avoid score
  dilution.
- If `failure_log` is empty but user provides a CI link, attempt to fetch the latest failure log via CI API (e.g.,
  GitHub Actions, Jenkins).
- Use `test_file_hint` to infer the source file under test (e.g., extract from import statements in the test file).
- **Detect test file changes** – Check if the test file itself (or any test helper) was modified between `pass_time` and
  `fail_time`. If yes, output a strong warning and add it as a candidate explanation.

### Step 1: Retrieve PRs in the Immediate Interval

- Call platform API (GitHub GraphQL / GitLab REST) to fetch all PRs merged into the target branch within
  `[pass_time - 1h, fail_time + 1h]`.
- For each PR, collect: number, title, author, merge time, merge commit SHA, all changed file paths, full diff text (for
  suspected files).
- For rebase/squash merges, also obtain the original commit list.
- If pagination exceeds `max_pr_count`, take only the most recent `max_pr_count` PRs and warn.

### Step 2: Enhanced Suspicion Scoring (0–100) for Immediate PRs

Same scoring as before, but with one extra sub‑dimension for **latent‑risk patterns** (used only when later expanding to
historic PRs). For immediate PRs, latent risk is not applied because we assume they are fresh.

| Dimension             | Weight | Scoring                                                               |
|-----------------------|--------|-----------------------------------------------------------------------|
| File Path Match       | 35%    | Exact hit 50, same dir 40, same module 30, indirect 15, none 0        |
| Exact Stack Match     | 30%    | Same line/function 50, same file diff line 30, symbol only 20, none 0 |
| Semantic Correlation  | 20%    | Jaccard similarity >0.4 → 50, 0.2–0.4 → 30, <0.2 → 10, none 0         |
| Dependency/Env Change | 10%    | Any dep/CI file changed → 50 (test+dep → capped 50)                   |
| Time Proximity        | 5%     | <6h → 50, <24h → 30, <72h → 15, >72h → 0                              |

**Suspicion level**: High ≥70, Medium 50–69, Low <50.

### Step 3: Immediate Candidate Evaluation

- Output top 3 PRs from the immediate interval with scores and evidence.
- If a PR scores ≥70, output it as the likely cause and provide suggested fix.
- **If any test file change was detected** in the interval, add a special candidate: “The test itself was modified – the
  failure may be due to a stricter assertion or changed setup.” Score it artificially as High (≥80) and place it at the
  top of candidates.

### Step 3b: Delayed‑Failure Analysis (only if no High‑score PR found in immediate interval)

When the highest score in the immediate interval is <70, expand the search:

1. **Fetch historic PRs** – Retrieve PRs merged in the window `[pass_time - historic_window_days, pass_time)`. Default
   `historic_window_days` = 7 (configurable).
2. **Score historic PRs** using the same dimensions, but with modifications:
    - **Time proximity** is replaced by **inverse age** – older PRs get lower raw score, but we add a **latent‑risk
      bonus**.
    - **Latent‑risk bonus** (extra 0–30 points) based on the nature of changes:
        - Concurrency changes (locks, threads, async) → +25
        - Resource management (file handles, db connections, memory) → +25
        - Lazy initialization / caching → +20
        - Configuration that becomes active only after a time‑based condition → +20
        - Dependency upgrade (if failure log shows new exception types) → +15
        - Otherwise 0.
    - **Formula**:
      `historic_score = (file_match + stack_match + semantic + dependency) * (0.7) + inverse_age_score + latent_bonus`  
      (Weights are slightly reduced because immediate causality is weaker.)
3. **Flag “delayed manifestation”** – For any historic PR scoring >50 after the bonus, mark it as a possible delayed
   cause and include it in the candidate list, clearly stating that it merged before the pass time but may have only
   recently started failing.

### Step 4: Output Candidate List & Verification Guidance

The output includes:

- Immediate‑interval candidates (if any)
- Delayed‑failure candidates (if applicable)
- A fallback section if nothing scores ≥50

For each candidate:

- PR number, title, author, merge time
- Score and level (with note if it's from the delayed‑analysis window)
- Detailed breakdown and evidence
- Suggested fix or next action

**Fallback** (when no candidate >50):

- Provide `git bisect` command from `pass_time` commit (good) to `fail_time` commit (bad).
- Suggest to inspect test file changes, CI environment diffs (images, runners), and external service health.
- Recommend manual binary search across a wider range if the failure is intermittent.

### Step 5 (Optional): Auto Verification (only if `enable_auto_verify=true`)

Same as before, but also allow verifying historic PRs (by checking out the commit before `pass_time`).

## Output Format (Enhanced JSON)

```json
{
  "repo": "string",
  "test_case": "string",
  "analysis_period": {
    "pass_time": "ISO 8601",
    "fail_time": "ISO 8601",
    "immediate_window": { "start": "ISO 8601", "end": "ISO 8601" },
    "historic_window": { "start": "ISO 8601", "end": "ISO 8601" }
  },
  "test_file_changed_in_interval": true,
  "candidates": [
    {
      "rank": 1,
      "source": "immediate",
      "pr_number": 452,
      "title": "string",
      "author": "string",
      "merged_at": "ISO 8601",
      "score": 85,
      "score_level": "high",
      "score_breakdown": { ... },
      "evidence": [ ... ],
      "suggested_fix": "string"
    },
    {
      "rank": 2,
      "source": "historic (delayed manifestation)",
      "pr_number": 398,
      "title": "string",
      "author": "string",
      "merged_at": "ISO 8601",
      "score": 67,
      "score_level": "medium",
      "delayed_bonus": 25,
      "latent_risk_type": "concurrency",
      "evidence": [ ... ],
      "suggested_fix": "string"
    }
  ],
  "fallback": {
    "reason": "No candidate scored >= 50",
    "bisect_command": "git bisect start <fail_commit> <pass_commit>",
    "manual_check_hints": [ ... ]
  },
  "auto_verify_results": [ ... ]
}