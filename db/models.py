# memebot3/db/models.py
"""
Declaración de tablas SQLAlchemy (async).

• Token     – metadata y señales de cada par evaluado
• Position  – posiciones abiertas/cerradas por el bot
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# ───────────────────────── Token ─────────────────────────
class Token(Base):
    __tablename__ = "tokens"

    # —— claves ——
    address: Mapped[str] = mapped_column(String, primary_key=True)
    symbol:  Mapped[Optional[str]] = mapped_column(String(16))
    name:    Mapped[Optional[str]] = mapped_column(String(64))

    # —— métricas on-chain ——
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _dt.datetime.now(_dt.timezone.utc),
    )

    liquidity_usd:   Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    volume_24h_usd:  Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    market_cap_usd:  Mapped[float] = mapped_column(Float, default=0.0, server_default="0")  # ← NUEVO
    holders:         Mapped[int]   = mapped_column(Integer, default=0)

    # —— señales ——
    rug_score:    Mapped[int]   = mapped_column(Integer, default=0)
    cluster_bad:  Mapped[bool]  = mapped_column(Boolean, default=False)
    social_ok:    Mapped[bool]  = mapped_column(Boolean, default=False)
    trend:        Mapped[Optional[str]] = mapped_column(String(8))
    insider_sig:  Mapped[bool]  = mapped_column(Boolean, default=False)
    score_total:  Mapped[int]   = mapped_column(Integer, default=0)

    # —— metadatos descubrimiento ——
    discovered_via: Mapped[Optional[str]]          = mapped_column(String(16))
    discovered_at:  Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True))

    # —— relaciones ——
    positions: Mapped[list["Position"]] = relationship(back_populates="token")
    revived:   Mapped["RevivedToken"]   = relationship(back_populates="token", uselist=False)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Token {self.symbol or self.address[:4]} "
            f"mcap={self.market_cap_usd:.0f} vol24h={self.volume_24h_usd:.0f}>"
        )

# ───────────────────────── Position ───────────────────────
class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String, ForeignKey("tokens.address"))
    symbol:  Mapped[Optional[str]] = mapped_column(String(16))
    qty:     Mapped[int] = mapped_column(Integer)  # lamports

    buy_price_usd: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _dt.datetime.now(_dt.timezone.utc),
    )

    closed:          Mapped[bool]              = mapped_column(Boolean, default=False)
    closed_at:       Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True))
    close_price_usd: Mapped[Optional[float]]   = mapped_column(Float)
    exit_tx_sig:     Mapped[Optional[str]]     = mapped_column(String(32))

    # —— resultado (‘win’ / ‘fail’ / ‘fail_timeout’) ——
    outcome: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)

    # —— relaciones ——
    token: Mapped["Token"] = relationship(back_populates="positions")

    highest_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Position {self.symbol or self.address[:4]} open={self.opened_at}>"

# ───────────────────────── RevivedToken ────────────────────────
class RevivedToken(Base):
    __tablename__ = "revived_tokens"

    token_address: Mapped[str] = mapped_column(
        String, ForeignKey("tokens.address"), primary_key=True
    )
    first_listed: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True))
    revived_at:   Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True))

    liq_revived:  Mapped[float] = mapped_column(Float, default=0.0)
    vol_revived:  Mapped[float] = mapped_column(Float, default=0.0)
    buyers_delta: Mapped[int]   = mapped_column(Integer, default=0)

    token: Mapped["Token"] = relationship(back_populates="revived", uselist=False)
