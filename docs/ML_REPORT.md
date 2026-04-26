# ML Report

- Metrics dir: `D:\Dev\Python\memebot3\data\metrics`
- Model meta: `ml\model.meta.json`

## Dataset Quality

- `passed`: `True`
- `reasons`: `[]`
- `rows`: `470`
- `positives`: `123`
- `unique_tokens`: `397`
- `realized_return_rows`: `470`
- `non_constant_numeric_features`: `14`
- `holdout_rows`: `284`
- `holdout_positives`: `68`
- `holdout_unique_tokens`: `237`

## Training Status

- `status`: `trained`
- `feature_set_hash`: `00938bb3f2`
- `split_meta`: `{'mode': 'walk_forward_grouped_by_mint', 'splits': 3, 'n_splits_requested': 5, 'min_train_blocks': 2, 'fallback_from_forward_holdout': True, 'forward_holdout_meta': {'mode': 'forward_holdout', 'cutoff': '2026-04-15 03:35:48.970781+00:00', 'tmin': '2026-04-16 20:54:41.026323+00:00', 'tmax': '2026-04-18 03:35:48.970781+00:00', 'train_mints': 0, 'val_mints': 397}}`
- `auc_pr_forward_or_cv_mean`: `0.3649551661025398`
- `precision_at_k_val`: `0.39285714285714285`

## Threshold

- `picked`: `0.6575990683372152`
- `objective_requested`: `expected_pnl_precision_floor`
- `objective_applied`: `expected_pnl_precision_floor`
- `activation_ready`: `True`
- `activation_reason`: `precision_floor_met`
- `precision_at_picked`: `0.6`
- `recall_at_picked`: `0.08823529411764706`
- `f1_at_picked`: `0.15384615384615385`
- `avg_realized_pnl_pct_at_picked`: `44.268367767333984`
- `total_realized_pnl_pct_points_at_picked`: `442.6836853027344`
- `selected_rows_at_picked`: `10`
- `realized_selected_rows_at_picked`: `10`

## Model Meta

- `activation_ready`: `True`
- `dataset_quality_passed`: `True`
- `model_selection_metric`: `avg_realized_pnl_pct_at_picked`
- `model_selection_score`: `44.268367767333984`
- `rows`: `470`
- `feature_set_hash`: `00938bb3f2`

## Validation Snapshot

- `rows`: `284`
- `realized_rows`: `284`
- `avg_realized_pnl_pct`: `-1.4171210715140838`
- `median_realized_pnl_pct`: `-0.54432671`
