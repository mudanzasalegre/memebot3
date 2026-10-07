# memebot3/labeler/win_labeler.py
import asyncio
import datetime as dt
import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from utils.time import utc_now
from db.database import async_init_db, SessionLocal, DB_PATH  # usa el mismo engine
from config.config import MAX_HOLDING_H, LABEL_GRACE_H, PROJECT_ROOT
from runtime.trade_learning import checked_position_outcome

log = logging.getLogger("labeler")

# --- parámetros de negocio ----------------------------
# Positive threshold is frozen in each checked pre-buy feature source.
MAX_H_HOLD = dt.timedelta(hours=MAX_HOLDING_H)
GRACE = dt.timedelta(hours=LABEL_GRACE_H)  # ej.: 2 h tras cierre


async def label_positions() -> None:
    """Label only checked terminal estimated-net paper outcomes; unknown stays unknown."""
    from db.models import Position  # import perezoso
    now = utc_now()

    async with SessionLocal() as s:
        # 1) cerradas sin outcome pasado el grace-period
        q = sa.select(Position).where(
            Position.outcome.is_(None),
            Position.closed.is_(True),
            Position.qty == 0,
            Position.closed_at.is_not(None),
            Position.closed_at < now - GRACE,
        )
        res = await s.execute(q)
        for pos in res.scalars():
            try:
                outcome = checked_position_outcome(pos, root=PROJECT_ROOT)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
                log.warning("[labeler] Financial outcome remains unknown %s: %s", pos.address[:6], type(exc).__name__)
                continue
            if outcome is not None:
                pos.outcome = outcome

        # A holding deadline is operational state, not a realized loss.
        q_open = sa.select(Position).where(
            Position.outcome.is_(None),
            Position.closed_at.is_(None),
            Position.opened_at < now - MAX_H_HOLD,
        )
        res2 = await s.execute(q_open)
        overdue = len(list(res2.scalars()))
        if overdue:
            log.warning("[labeler] %d open positions exceed the holding deadline; no realized labels assigned", overdue)

        await s.commit()


async def weekly_outcome_log() -> None:
    """
    Logea un resumen de outcomes de la última semana:
    %win / %fail / %timeout y totales.
    """
    from db.models import Position  # import perezoso
    now = utc_now()
    since = now - dt.timedelta(days=7)

    async with SessionLocal() as s:
        # Outcomes con timestamp reciente: usamos closed_at si existe;
        # para fail_timeout (sin closed_at), usamos opened_at como referencia.
        cond_recent = sa.or_(
            sa.and_(Position.closed_at.is_not(None), Position.closed_at >= since),
            sa.and_(Position.closed_at.is_(None), Position.opened_at >= since),
        )

        q = (
            sa.select(Position.outcome, sa.func.count().label("n"))
            .where(Position.outcome.is_not(None), cond_recent)
            .group_by(Position.outcome)
        )

        res = await s.execute(q)
        rows = res.all()

        counts = {"win": 0, "fail": 0, "fail_timeout": 0}
        total = 0
        for outcome, n in rows:
            if outcome in counts:
                counts[outcome] += int(n or 0)
                total += int(n or 0)

        if total == 0:
            log.info(
                "[labeler] Últimos 7 días: sin posiciones etiquetadas (total=0)."
            )
            return

        pct_win = 100.0 * counts["win"] / total if total else 0.0
        pct_fail = 100.0 * counts["fail"] / total if total else 0.0
        pct_to = 100.0 * counts["fail_timeout"] / total if total else 0.0

        log.info(
            "[labeler] Últimos 7 días: total=%d | win=%d (%.1f%%) | fail=%d (%.1f%%) | timeout=%d (%.1f%%)",
            total,
            counts["win"],
            pct_win,
            counts["fail"],
            pct_fail,
            counts["fail_timeout"],
            pct_to,
        )


async def main() -> None:
    # garantiza que la BD existe si se ejecuta stand-alone
    await async_init_db()
    await label_positions()
    await weekly_outcome_log()


if __name__ == "__main__":
    asyncio.run(main())
