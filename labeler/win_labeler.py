# memebot3/labeler/win_labeler.py
import asyncio
import datetime as dt
import logging
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from utils.time import utc_now
from db.database import async_init_db, SessionLocal, DB_PATH  # usa el mismo engine
from config.config import WIN_PCT, MAX_HOLDING_H, LABEL_GRACE_H

log = logging.getLogger("labeler")

# --- parámetros de negocio ----------------------------
WIN_THRESH = Decimal(WIN_PCT)  # +X %
MAX_H_HOLD = dt.timedelta(hours=MAX_HOLDING_H)
GRACE = dt.timedelta(hours=LABEL_GRACE_H)  # ej.: 2 h tras cierre


async def label_positions() -> None:
    """Aplica 'win' / 'fail' / 'fail_timeout' a posiciones ya cerradas o caducadas."""
    from db.models import Position  # import perezoso
    now = utc_now()

    async with SessionLocal() as s:
        # 1) cerradas sin outcome pasado el grace-period
        q = sa.select(Position).where(
            Position.outcome.is_(None),
            Position.closed_at.is_not(None),
            Position.closed_at < now - GRACE,
        )
        res = await s.execute(q)
        for pos in res.scalars():
            pnl_pct = (
                (pos.close_price_usd - pos.buy_price_usd) / pos.buy_price_usd
                if pos.close_price_usd and pos.buy_price_usd
                else 0
            )
            pos.outcome = "win" if pnl_pct >= WIN_THRESH else "fail"

        # 2) abiertas demasiado tiempo → fail_timeout
        q_open = sa.select(Position).where(
            Position.outcome.is_(None),
            Position.closed_at.is_(None),
            Position.opened_at < now - MAX_H_HOLD,
        )
        res2 = await s.execute(q_open)
        for pos in res2.scalars():
            pos.outcome = "fail_timeout"

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
