from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd
from features.context_encoding import augment_context_frame
from features.numeric_encoding import augment_numeric_frame


def coerce_feature_frame(frame: pd.DataFrame, feature_names: Sequence[str]) -> pd.DataFrame:
    """
    Deriva las parejas T0 de valor/ausencia antes de la imputación a cero.
    Entrenamiento e inferencia comparten el contrato; las listas de features
    legacy sin indicadores conservan su interpretación original.
    """
    cols = list(feature_names)
    X = augment_numeric_frame(augment_context_frame(frame, cols), cols).reindex(columns=cols).copy()
    # Derived numeric/context columns are already numeric; avoid converting
    # every indicator again on each latency-sensitive one-row inference.
    nonnumeric = [name for name, dtype in X.dtypes.items() if not pd.api.types.is_numeric_dtype(dtype)]
    if nonnumeric:
        X[nonnumeric] = X[nonnumeric].apply(pd.to_numeric, errors="coerce")
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return X.astype(np.float32)


__all__ = ["coerce_feature_frame"]
