"""
Feature-store package.

🔹 `builder.build_feature_vector(token_dict)` → pd.Series  
🔹 `store.append(row, label[, pnl])`           → guarda en Parquet  
🔹 `store.update_pnl(token_addr, pnl_pct)`     → registra el PnL final
"""
from .builder import build_feature_vector            # noqa: F401
from . import store                                  # noqa: F401
