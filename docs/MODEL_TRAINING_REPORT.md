# Model Training Report

- Generated at UTC: `2026-07-07T13:16:43.866488+00:00`
- Promotion attempted: `false`
- Enforcement enabled: `false`
- Ready for enforcement: `False`
- Critical warnings: `in_sample_only, low_precision_at_k, not_enough_rows, not_ready_for_enforcement, single_class, unstable_by_lane`

| Family | Status | Rows | Warnings | Critical |
|---|---|---:|---|---|
| risk | ok | 3479 | in_sample_only, low_precision_at_k, not_ready_for_enforcement, single_class | in_sample_only, low_precision_at_k, not_ready_for_enforcement, single_class |
| ev | ok | 3479 | in_sample_only, not_ready_for_enforcement | in_sample_only, not_ready_for_enforcement |
| runner | ok | 3479 | in_sample_only, low_precision_at_k, not_ready_for_enforcement, single_class, unstable_by_lane | in_sample_only, low_precision_at_k, not_ready_for_enforcement, single_class, unstable_by_lane |
| continuation | unknown | 0 | in_sample_only, not_enough_rows, not_ready_for_enforcement, single_class | in_sample_only, not_enough_rows, not_ready_for_enforcement, single_class |

## Notes

- These models are trained for reports only.
- Validation is marked in-sample unless a future holdout path is added.
- Critical warnings block enforcement in `strategy_quality_gate`.
