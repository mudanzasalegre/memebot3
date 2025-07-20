# memebot3/utils/logger.py
"""
utils.logger
~~~~~~~~~~~~
• Rotación horaria con run-id incremental
• Filtro anti-spam que suprime repeticiones idénticas ≤ 30 s
• Helper warn_if_nulls()
• NEW 2025-07-20 : `log_funnel(stats)` → imprime el embudo del bot
"""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
import re
import time
from typing import Any, Mapping

from config.config import CFG

# ——————————————————— formato base ———————————————————
_LOG_FORMAT = "%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
_DATE_FMT   = "%H:%M:%S"

# ——————————————————— anti-spam filter ——————————————————
class DedupFilter(logging.Filter):
    """
    Suprime repetición exacta del mismo mensaje + level en ≤ 30 s.
    """
    _cache: dict[str, float] = {}
    _window = 30.0            # segundos

    def filter(self, record: logging.LogRecord) -> bool:   # noqa: D401
        key = f"{record.levelno}:{record.getMessage()}"
        now = time.time()
        last = self._cache.get(key, 0.0)
        self._cache[key] = now
        return (now - last) > self._window


# ——————————————————— run-id helper ——————————————————
def _next_run_id(logs_path: pathlib.Path, today_prefix: str) -> int:
    pat = re.compile(fr"^{today_prefix}\d{{4}}-(\d+)\.txt$")
    run_ids = [
        int(m.group(1))
        for f in logs_path.iterdir()
        if (m := pat.match(f.name))
    ]
    return max(run_ids, default=0) + 1


# ——————————————————— file-handler ——————————————————
class HourlySplitFileHandler(logging.Handler):
    """
    Rota cada hora; mantiene el run-id durante todo el proceso.
    """
    def __init__(self, logs_path: pathlib.Path, run_id: int) -> None:
        super().__init__()
        self.logs_path = logs_path
        self.run_id = run_id
        self.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FMT))
        self._open_file()

    def _open_file(self) -> None:
        now = dt.datetime.now()
        self._period_start = now.replace(minute=0, second=0, microsecond=0)
        fname = f"{self._period_start:%y%m%d%H%M}-{self.run_id}.txt"
        self._file = open(self.logs_path / fname, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        if dt.datetime.now() - self._period_start >= dt.timedelta(hours=1):
            self._file.close()
            self._open_file()
        self._file.write(self.format(record) + "\n")
        self._file.flush()

    def close(self) -> None:
        try:
            if not self._file.closed:
                self._file.close()
        finally:
            super().close()


# ——————————————————— observabilidad ——————————————————
_CRITICAL_NUMERIC = {"liquidity_usd", "volume_24h_usd"}

def warn_if_nulls(data: Mapping[str, Any], *, context: str = "") -> None:
    """
    Log-warning si algún campo crítico (liq/vol) está nulo / 0 / NaN.
    """
    missing = [
        k for k in _CRITICAL_NUMERIC
        if not data.get(k) or (hasattr(data.get(k), "size") and data.get(k) != data.get(k))
    ]
    if missing:
        logging.getLogger("data").warning(
            "Campos críticos nulos %s — %s",
            ",".join(missing),
            context,
        )

# ——————————————————— nuevo helper funnel —————————————————
def log_funnel(stats: Mapping[str, int]) -> None:
    """
    Imprime un resumen compacto del embudo de eventos del bot.

    Parameters
    ----------
    stats : dict
        Debe incluir las claves:
        raw_discovered · incomplete · filtered_out · ai_pass · bought · sold
    """
    tpl = (
        "Funnel | discovered={raw_discovered}  incomplete={incomplete}  "
        "filtered={filtered_out}  ai_pass={ai_pass}  bought={bought}  sold={sold}"
    )
    logging.getLogger("funnel").info(tpl.format(**stats))

# ——————————————————— init público ——————————————————
def enable_file_logging() -> int:
    logs_path = CFG.LOG_PATH
    logs_path.mkdir(parents=True, exist_ok=True)

    today_prefix = dt.datetime.now().strftime("%y%m%d")
    run_id = _next_run_id(logs_path, today_prefix)

    root = logging.getLogger()
    handler = HourlySplitFileHandler(logs_path, run_id)
    handler.addFilter(DedupFilter())          # ★ anti-spam
    root.addHandler(handler)
    root.setLevel(getattr(logging, CFG.LOG_LEVEL, logging.INFO))
    return run_id


__all__ = ["enable_file_logging", "warn_if_nulls", "log_funnel"]
