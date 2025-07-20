# memebot3/labeler/win_labeler.py
import asyncio, datetime as dt, logging
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from utils.time import utc_now
from db.database import async_init_db, SessionLocal, DB_PATH      # usa el mismo engine
from config.config import WIN_PCT, MAX_HOLDING_H, LABEL_GRACE_H

log = logging.getLogger("labeler")

# --- parámetros de negocio ----------------------------
WIN_THRESH = Decimal(WIN_PCT)          # +X %
MAX_H_HOLD = dt.timedelta(hours=MAX_HOLDING_H)
GRACE      = dt.timedelta(hours=LABEL_GRACE_H)   # ej.: 2 h tras cierre


async def label_positions() -> None:
    """Aplica 'win' / 'fail' / 'fail_timeout' a posiciones ya cerradas o caducadas."""
    from db.models import Position                        # import perezoso
    now = utc_now()

    async with SessionLocal() as s:
        # 1) cerradas sin outcome pasado el grace-period
        q = sa.select(Position).where(
            Position.outcome.is_(None),
            Position.closed_at.is_not(None),
            Position.closed_at < now - GRACE,
        )
        for pos in (await s.execute(q)).scalars():
            pnl_pct = (
                (pos.close_price_usd - pos.buy_price_usd) / pos.buy_price_usd
                if pos.close_price_usd and pos.buy_price_usd else 0
            )
            pos.outcome = "win" if pnl_pct >= WIN_THRESH else "fail"

        # 2) abiertas demasiado tiempo → fail_timeout
        q_open = sa.select(Position).where(
            Position.outcome.is_(None),
            Position.closed_at.is_(None),
            Position.opened_at < now - MAX_H_HOLD,
        )
        for pos in (await s.execute(q_open)).scalars():
            pos.outcome = "fail_timeout"

        await s.commit()


async def main() -> None:
    # garantiza que la BD existe si se ejecuta stand-alone
    await async_init_db()
    await label_positions()

if __name__ == "__main__":
    asyncio.run(main())
