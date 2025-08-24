# memebot3/db/models.py
"""
Declaración de tablas SQLAlchemy (async).

• Token     – metadata y señales de cada par evaluado
• Position  – posiciones abiertas/cerradas por el bot

Notas de esquema (2025-08)
──────────────────────────
• Position incorpora campos de trazabilidad de precio:
    - price_source_at_buy / price_source_at_close
• Señales para “liquidity crush” y auditoría:
    - buy_liquidity_usd / buy_market_cap_usd / buy_volume_24h_usd
• Trazabilidad de ejecución:
    - buy_tx_sig / exit_tx_sig (anchura aumentada para firmas de Solana)
• Métrica útil para trailing y telemetría:
    - peak_price_usd (precio máximo observado durante la vida de la posición)
    - highest_pnl_pct (histórico de PnL máximo, en %)
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from sqlalchemy import (
    Boolean,
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
    market_cap_usd:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
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
            f"mcap={self.market_cap_usd or 0:.0f} vol24h={self.volume_24h_usd:.0f}>"
        )


# ───────────────────────── Position ───────────────────────
class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Mint SPL explícito (opcional). Mantiene compatibilidad hacia atrás.
    token_mint: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Nota: en tu pipeline actual, 'address' ya es el mint SPL. Se conserva por compatibilidad.
    address: Mapped[str] = mapped_column(String, ForeignKey("tokens.address"))
    symbol:  Mapped[Optional[str]] = mapped_column(String(16))

    # Cantidad en lamports del token (no SOL)
    qty:     Mapped[int] = mapped_column(Integer)

    # —— entrada (compra) ——
    buy_price_usd: Mapped[float] = mapped_column(Float)
    price_source_at_buy: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    buy_tx_sig: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)  # firma de compra (Solana ~88 chars)

    # Métricas del par en el momento de la compra (para auditoría / reglas como liquidity-crush)
    buy_liquidity_usd:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    buy_market_cap_usd:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    buy_volume_24h_usd:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Pico de precio observado durante la vida de la posición (útil para trailing)
    peak_price_usd: Mapped[float] = mapped_column(Float, default=0.0)

    opened_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _dt.datetime.now(_dt.timezone.utc),
    )

    # —— cierre (venta) ——
    closed:          Mapped[bool]                   = mapped_column(Boolean, default=False)
    closed_at:       Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True))
    close_price_usd: Mapped[Optional[float]]        = mapped_column(Float, nullable=True)
    exit_tx_sig:     Mapped[Optional[str]]          = mapped_column(String(96), nullable=True)
    price_source_at_close: Mapped[Optional[str]]    = mapped_column(String(16), nullable=True)
    exit_reason:     Mapped[Optional[str]]          = mapped_column(String(24), nullable=True)  # p.ej. TAKE_PROFIT / EARLY_DROP / TIMEOUT

    # —— resultado (‘win’ / ‘fail’ / ‘fail_timeout’) ——
    outcome: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)

    # Métrica histórica de PnL máximo (en %)
    highest_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)

    # —— relaciones ——
    token: Mapped["Token"] = relationship(back_populates="positions")

    def __repr__(self) -> str:  # pragma: no cover
        alias = (self.token_mint or self.address or "")[:4]
        return f"<Position {self.symbol or alias} open={self.opened_at}>"

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
