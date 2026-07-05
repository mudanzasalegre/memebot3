# ML Report

- Metrics dir: `D:\Dev\Python\memebot3\data\metrics`
- Model meta: `ml\model.meta.json`

## Dataset Quality

- `passed`: `True`
- `reasons`: `[]`
- `rows`: `1508`
- `positives`: `87`
- `unique_tokens`: `1498`
- `realized_return_rows`: `1508`
- `non_constant_numeric_features`: `32`
- `holdout_rows`: `905`
- `holdout_positives`: `43`
- `holdout_unique_tokens`: `898`

## Training Status

- `status`: `trained`
- `feature_set_hash`: `9408e98944`
- `split_meta`: `{'mode': 'walk_forward_grouped_by_mint', 'splits': 3, 'n_splits_requested': 5, 'min_train_blocks': 2, 'fallback_from_forward_holdout': True, 'forward_holdout_meta': {'mode': 'forward_holdout', 'cutoff': '2026-06-04 20:50:43.534448+00:00', 'tmin': '2026-06-06 12:06:34.013501+00:00', 'tmax': '2026-06-07 20:50:43.534448+00:00', 'train_mints': 0, 'val_mints': 1498}}`
- `auc_pr_forward_or_cv_mean`: `0.15463673491054333`
- `precision_at_k_val`: `0.17777777777777778`

## Threshold

- `picked`: `0.5921693260878585`
- `objective_requested`: `expected_pnl_precision_floor`
- `objective_applied`: `expected_pnl`
- `activation_ready`: `False`
- `activation_reason`: `non_positive_expected_pnl`
- `precision_at_picked`: `0.21052631578947367`
- `recall_at_picked`: `0.37209302325581395`
- `f1_at_picked`: `0.2689075630252101`
- `avg_realized_pnl_pct_at_picked`: `-8.29234277731494`
- `total_realized_pnl_pct_points_at_picked`: `-630.2180510759354`
- `selected_rows_at_picked`: `76`
- `realized_selected_rows_at_picked`: `76`

## Model Meta

- Sin datos

## Validation Snapshot

- `rows`: `905`
- `realized_rows`: `905`
- `avg_realized_pnl_pct`: `-9.90268282478104`
- `median_realized_pnl_pct`: `0.0`
