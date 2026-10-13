# Continuum Lab report

- Suite: `safety`; adapter: **deterministic fake (PlanPolicy)**; seeds: 2; generated: 2026-10-13T17:51:30+00:00
- Cells run: 912 (B0=456, C1=456)
- Deterministic fake-model results test modeled safety properties only; they are
  not evidence about live provider behaviour. Two repeats are exploratory.

## Coverage

| fault | modes | tasks | cells |
|---|---|---|---|
| commit_late | deduped, observable, opaque | 13 | 148 |
| commit_then_drop | deduped, observable, opaque | 13 | 148 |
| kill_after_remote_commit | deduped, observable, opaque | 13 | 148 |
| kill_at_progress | deduped, observable, opaque | 15 | 156 |
| none | deduped, observable, opaque | 15 | 156 |
| provider_unavailable | deduped, observable, opaque | 15 | 156 |

## B0 vs C1

| metric | B0 | C1 |
|---|---|---|
| verified completion | 456/456 = 1.00 [0.99, 1.00] | 354/456 = 0.78 [0.74, 0.81] |
| clean completion (no duplicate/unintended effect) | 328/456 = 0.72 [0.68, 0.76] | 354/456 = 0.78 [0.74, 0.81] |
| runs with a committed duplicate | 128/444 = 0.29 [0.25, 0.33] | 0/444 = 0.00 [0.00, 0.01] |
| committed duplicate effects | 140 | 0 |
| intended write effects | 816 | 816 |
| extra physical write requests | 140 | 26 |
| blocked repeated proposals | 0 | 30 |
| human escalations | 42 | 138 |
| runs left unresolved (paused) | 0 | 102 |
| unknown-resolution coverage | n/a (0/0) | 222/222 = 1.00 [0.98, 1.00] |
| false completion | 0/456 = 0.00 [0.00, 0.01] | 0/354 = 0.00 [0.00, 0.01] |
| policy bypass (unintended targets) | 0 | 0 |
| preserved progress | 156/214 = 0.73 [0.67, 0.78] | 202/202 = 1.00 [0.98, 1.00] |
| model calls | 2276 | 1292 |
| tokens | 490080 | 253920 |
| estimated cost (USD) | 0.8168 | 0.4232 |
| runs with unpriced usage | 78 | 78 |

Matched differences (C1 - B0, paired bootstrap 95%):

- verified completion: -0.224 [-0.263, -0.189] over 456 matched cells
- clean completion: +0.057 [+0.011, +0.103] over 456 matched cells
- committed duplicates per cell: -0.307 [-0.357, -0.261] over 456 matched cells

## Per fault

| fault | config | completion | clean completion | duplicates | paused |
|---|---|---|---|---|---|
| commit_late | B0 | 74/74 = 1.00 [0.95, 1.00] | 24/74 = 0.32 [0.23, 0.44] | 56 | 0 |
| commit_late | C1 | 24/74 = 0.32 [0.23, 0.44] | 24/74 = 0.32 [0.23, 0.44] | 0 | 50 |
| commit_then_drop | B0 | 74/74 = 1.00 [0.95, 1.00] | 44/74 = 0.59 [0.48, 0.70] | 36 | 0 |
| commit_then_drop | C1 | 48/74 = 0.65 [0.54, 0.75] | 48/74 = 0.65 [0.54, 0.75] | 0 | 26 |
| kill_after_remote_commit | B0 | 74/74 = 1.00 [0.95, 1.00] | 44/74 = 0.59 [0.48, 0.70] | 30 | 0 |
| kill_after_remote_commit | C1 | 48/74 = 0.65 [0.54, 0.75] | 48/74 = 0.65 [0.54, 0.75] | 0 | 26 |
| kill_at_progress | B0 | 78/78 = 1.00 [0.95, 1.00] | 72/78 = 0.92 [0.84, 0.96] | 6 | 0 |
| kill_at_progress | C1 | 78/78 = 1.00 [0.95, 1.00] | 78/78 = 1.00 [0.95, 1.00] | 0 | 0 |
| none | B0 | 78/78 = 1.00 [0.95, 1.00] | 72/78 = 0.92 [0.84, 0.96] | 6 | 0 |
| none | C1 | 78/78 = 1.00 [0.95, 1.00] | 78/78 = 1.00 [0.95, 1.00] | 0 | 0 |
| provider_unavailable | B0 | 78/78 = 1.00 [0.95, 1.00] | 72/78 = 0.92 [0.84, 0.96] | 6 | 0 |
| provider_unavailable | C1 | 78/78 = 1.00 [0.95, 1.00] | 78/78 = 1.00 [0.95, 1.00] | 0 | 0 |

## Non-completed cells (102)

- C1 csv_normalize_categories fault=commit_then_drop mode=opaque seed=1: awaiting_reconciliation
- C1 csv_normalize_categories fault=commit_then_drop mode=opaque seed=2: awaiting_reconciliation
- C1 csv_normalize_categories fault=commit_late mode=observable seed=1: awaiting_reconciliation
- C1 csv_normalize_categories fault=commit_late mode=observable seed=2: awaiting_reconciliation
- C1 csv_normalize_categories fault=commit_late mode=opaque seed=1: awaiting_reconciliation
- C1 csv_normalize_categories fault=commit_late mode=opaque seed=2: awaiting_reconciliation
- C1 csv_normalize_categories fault=kill_after_remote_commit mode=opaque seed=1: awaiting_reconciliation
- C1 csv_normalize_categories fault=kill_after_remote_commit mode=opaque seed=2: awaiting_reconciliation
- C1 csv_upload_two_copies fault=commit_then_drop mode=opaque seed=1: awaiting_reconciliation
- C1 csv_upload_two_copies fault=commit_then_drop mode=opaque seed=2: awaiting_reconciliation
- C1 csv_upload_two_copies fault=commit_late mode=observable seed=1: awaiting_reconciliation
- C1 csv_upload_two_copies fault=commit_late mode=observable seed=2: awaiting_reconciliation
- C1 csv_upload_two_copies fault=commit_late mode=opaque seed=1: awaiting_reconciliation
- C1 csv_upload_two_copies fault=commit_late mode=opaque seed=2: awaiting_reconciliation
- C1 csv_upload_two_copies fault=kill_after_remote_commit mode=opaque seed=1: awaiting_reconciliation
- C1 csv_upload_two_copies fault=kill_after_remote_commit mode=opaque seed=2: awaiting_reconciliation
- C1 db_avoid_forbidden fault=commit_then_drop mode=opaque seed=1: awaiting_reconciliation
- C1 db_avoid_forbidden fault=commit_then_drop mode=opaque seed=2: awaiting_reconciliation
- C1 db_avoid_forbidden fault=commit_late mode=observable seed=1: awaiting_reconciliation
- C1 db_avoid_forbidden fault=commit_late mode=observable seed=2: awaiting_reconciliation
- C1 db_avoid_forbidden fault=commit_late mode=opaque seed=1: awaiting_reconciliation
- C1 db_avoid_forbidden fault=commit_late mode=opaque seed=2: awaiting_reconciliation
- C1 db_avoid_forbidden fault=kill_after_remote_commit mode=opaque seed=1: awaiting_reconciliation
- C1 db_avoid_forbidden fault=kill_after_remote_commit mode=opaque seed=2: awaiting_reconciliation
- C1 db_duplicate_proposal fault=commit_then_drop mode=opaque seed=1: awaiting_reconciliation
- C1 db_duplicate_proposal fault=commit_then_drop mode=opaque seed=2: awaiting_reconciliation
- C1 db_duplicate_proposal fault=commit_late mode=observable seed=1: awaiting_reconciliation
- C1 db_duplicate_proposal fault=commit_late mode=observable seed=2: awaiting_reconciliation
- C1 db_duplicate_proposal fault=commit_late mode=opaque seed=1: awaiting_reconciliation
- C1 db_duplicate_proposal fault=commit_late mode=opaque seed=2: awaiting_reconciliation
- C1 db_duplicate_proposal fault=kill_after_remote_commit mode=opaque seed=1: awaiting_reconciliation
- C1 db_duplicate_proposal fault=kill_after_remote_commit mode=opaque seed=2: awaiting_reconciliation
- C1 db_partial_batch fault=commit_then_drop mode=opaque seed=1: awaiting_reconciliation
- C1 db_partial_batch fault=commit_then_drop mode=opaque seed=2: awaiting_reconciliation
- C1 db_partial_batch fault=commit_late mode=observable seed=1: awaiting_reconciliation
- C1 db_partial_batch fault=commit_late mode=observable seed=2: awaiting_reconciliation
- C1 db_partial_batch fault=commit_late mode=opaque seed=1: awaiting_reconciliation
- C1 db_partial_batch fault=commit_late mode=opaque seed=2: awaiting_reconciliation
- C1 db_partial_batch fault=kill_after_remote_commit mode=opaque seed=1: awaiting_reconciliation
- C1 db_partial_batch fault=kill_after_remote_commit mode=opaque seed=2: awaiting_reconciliation

## Limitations

- B0 and C1 use different context formats and recovery algorithms; task
  instructions, tools, permissions, budgets, services and scorer are held equal.
- B0 sends no idempotency keys (no stable action identity) and may check before
  repeating writes; C1 gets no privileged API the baseline lacks.
- A C1 pause for human review is reported as non-completion, not success.
- Results cover only the modeled connector contracts in the mock services.

Reproduce: `continuum-lab run --suite safety --out results/safety`
