"""Restore checked original financial inputs before shared cohort selection.

Undeclared historical/shadow rows retain their diagnostic semantics. Neither
this preparation nor its report authenticates live execution or profitability.
"""
from copy import deepcopy

import numpy as np
import pandas as pd

from features.builder import COLUMNS
from features.auxiliary_semantics import PROOF_COLUMN
from features.token_clock_semantics import PROOF_COLUMN as CLOCK_PROOF
from features.valuation_semantics import PROOF_COLUMN as VALUATION_PROOF
from ml.financial_targets import checked_financial_frame, declared_financial_rows


RETURN_COLUMNS = ('realized_pnl_pct', 'total_pnl_pct', 'pnl_pct',
                  'target_total_pnl_pct', 'label')
SELECTION_COLUMNS = frozenset(COLUMNS) | {
    PROOF_COLUMN, CLOCK_PROOF, VALUATION_PROOF, 'sample_type',
    'outcome_return_basis', 'outcome_execution_proof', 'outcome_trade_id',
    'outcome_source_sha256', 'outcome_closed_at', 'target_total_pnl_pct',
}


def prepare_cohort_sources(frame):
    """Validate all declared identities before any label/cohort row removal.

    Conflicting copies reject their whole identity, even across partitions.
    Valid duplicates collapse to their first position. Temporary positional
    bookkeeping never becomes a model input; external indices may repeat.
    """
    if not frame.columns.is_unique:
        raise ValueError('Financial cohort columns must be unique')
    declared = declared_financial_rows(frame).to_numpy(dtype=bool)
    base = {'source_rows': len(frame), 'declared_rows': int(declared.sum()),
        'mode': 'original_financial_inputs_before_cohort_not_legacy_profit_authentication'}
    if not declared.any():
        return frame.copy(), {**base, 'restored_rows': 0, 'legacy_rows': len(frame),
            'rejected_rows': 0, 'duplicates_removed': 0, 'financial_validation': None}

    positions = np.flatnonzero(declared)
    checked, validation = checked_financial_frame(frame.iloc[positions].copy())
    original_records = frame.to_dict(orient='records')
    first_position = {}
    for position in positions:
        identity = original_records[position].get('outcome_trade_id')
        if isinstance(identity, str) and identity:
            first_position.setdefault(identity, int(position))
    records = [(position, row) for position, row in enumerate(original_records)
               if not declared[position]]
    records.extend((first_position[row['outcome_trade_id']], row)
                   for row in checked.to_dict(orient='records'))
    records.sort(key=lambda item: item[0])
    columns = frame.columns.union(checked.columns, sort=False) if not checked.empty else frame.columns
    out = pd.DataFrame([row for _, row in records], columns=columns,
                       index=frame.index.take([position for position, _ in records]))
    for column in RETURN_COLUMNS:
        out[column] = (pd.to_numeric(out[column], errors='coerce').astype('float64')
                       if column in out else np.nan)
    out.attrs = deepcopy(frame.attrs)
    return out, {**base, 'restored_rows': len(checked), 'legacy_rows': int((~declared).sum()),
        'rejected_rows': int(declared.sum()) - len(checked) - validation['duplicates_removed'],
        'duplicates_removed': validation['duplicates_removed'], 'financial_validation': validation}


def cohort_input_view(frame):
    """Financial selectors read only frozen predictors and checked metadata.

    Unowned physical aliases remain in the source frame as diagnostics, but
    cannot fill an original missing predictor. Legacy rows keep old semantics.
    """
    declared = declared_financial_rows(frame).to_numpy(dtype=bool)
    if not declared.any():
        return frame
    records = [{key: value for key, value in row.items() if key in SELECTION_COLUMNS}
               if declared[position] else row
               for position, row in enumerate(frame.to_dict(orient='records'))]
    out = pd.DataFrame(records, columns=frame.columns, index=frame.index)
    out.attrs = deepcopy(frame.attrs)
    return out
