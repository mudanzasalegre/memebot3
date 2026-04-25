from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


def coerce_feature_frame(frame: pd.DataFrame, feature_names: Sequence[str]) -> pd.DataFrame:
    """
    Alinea columnas, convierte a numérico y aplica la misma imputación que usa
    inferencia en tiempo real: NaN -> 0.0.
    """
    cols = list(feature_names)
    X = frame.reindex(columns=cols).copy()
    X = X.apply(pd.to_numeric, errors="coerce").fillna(0.0)
    return X.astype(np.float32)


__all__ = ["coerce_feature_frame"]
