# memebot3/db/models.py
"""
Declaración de tablas SQLAlchemy (async).

• Token       – metadata y señales de cada par evaluado
• Position    – posiciones abiertas/cerradas por el bot
• RevivedToken – marcas de “revivals” de pools/tokens

Notas de esquema (2025-08)
──────────────────────────
• Position incorpora campos de trazabilidad de precio:
    - price_source_at_buy / price_source_at_close
• Señales para “liquidity crush” y auditoría:
    - buy_liquidity_usd / buy_market_cap_usd / buy_volume_24h_usd
• Trazabilidad de ejecución:
    - buy_tx_sig / exit_tx_sig (anchura aumentada para firmas de Solana)
• Métricas útiles para trailing y telemetría:
    - peak_price_usd (precio máximo observado durante la vida de la posición)
    - highest_pnl_pct (histórico de PnL máximo, en %)

Actualización 2025-08-28
────────────────────────
• Añadidos en Position:
    - partial_taken: bool (marca de TP parcial)
    - peak_price: float (columna adicional; se mantiene peak_price_usd)
• Alias de compatibilidad (no columnas nuevas):
    - qty_lamports  ↔ qty
    - price_source_close ↔ price_source_at_close
    - price_source       ↔ price_source_at_buy
    - liq_at_buy_usd     ↔ buy_liquidity_usd
    - peak_price_prop (propiedad) ↔ peak_price_usd (además de la col. peak_price)

Notas
─────
• Mantén coherente la longitud de address/mint: Solana ~44 chars; se permite holgura.
• Los DateTime son timezone-aware (UTC).
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional, List

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


# ───────────────────────── helpers ─────────────────────────
def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ───────────────────────── Token ─────────────────────────
class Token(Base):
    __tablename__ = "tokens"
    __table_args__ = (
        Index("ix_tokens_created_at", "created_at"),
        Index("ix_tokens_symbol", "symbol"),
    )

    # —— claves ——
    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol:  Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    name:    Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # —— métricas on-chain ——
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    liquidity_usd:   Mapped[float] = mapped_column(Float, default=0.0)
    volume_24h_usd:  Mapped[float] = mapped_column(Float, default=0.0)
    market_cap_usd:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    holders:         Mapped[int]   = mapped_column(Integer, default=0)

    # —— señales ——
    rug_score:    Mapped[int]   = mapped_column(Integer, default=0)          # 0..100 (depende de tu fuente)
    cluster_bad:  Mapped[bool]  = mapped_column(Boolean, default=False)
    social_ok:    Mapped[bool]  = mapped_column(Boolean, default=False)
    trend:        Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    insider_sig:  Mapped[bool]  = mapped_column(Boolean, default=False)
    score_total:  Mapped[int]   = mapped_column(Integer, default=0)

    # —— metadatos descubrimiento ——
    discovered_via: Mapped[Optional[str]]          = mapped_column(String(16), nullable=True)
    discovered_at:  Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # —— relaciones ——
    positions: Mapped[List["Position"]] = relationship(back_populates="token")
    revived:   Mapped[Optional["RevivedToken"]] = relationship(
        back_populates="token", uselist=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Token {self.symbol or self.address[:4]} "
            f"mcap={int(self.market_cap_usd or 0):,} "
            f"vol24h={int(self.volume_24h_usd or 0):,}>"
        )


# ───────────────────────── Position ───────────────────────
class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_open", "closed", "opened_at"),
        Index("ix_positions_token", "address"),
        Index("ix_positions_token_mint", "token_mint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Mint SPL explícito (opcional). Mantiene compatibilidad hacia atrás.
    token_mint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Nota: en tu pipeline actual, 'address' ya es el mint SPL. Se conserva por compatibilidad.
    address: Mapped[str] = mapped_column(String(64), ForeignKey("tokens.address"), index=True)
    symbol:  Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

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
        DateTime(timezone=True), default=_utcnow
    )

    # —— cierre (venta) ——
    closed:          Mapped[bool]                   = mapped_column(Boolean, default=False, index=True)
    closed_at:       Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    close_price_usd: Mapped[Optional[float]]        = mapped_column(Float, nullable=True)
    exit_tx_sig:     Mapped[Optional[str]]          = mapped_column(String(96), nullable=True)
    price_source_at_close: Mapped[Optional[str]]    = mapped_column(String(16), nullable=True)
    exit_reason:     Mapped[Optional[str]]          = mapped_column(String(24), nullable=True)  # p.ej. TAKE_PROFIT / EARLY_DROP / TIMEOUT

    # —— resultado (‘win’ / ‘fail’ / ‘fail_timeout’) ——
    outcome: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)

    # Métrica histórica de PnL máximo (en %)
    highest_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)

    # —— NUEVOS CAMPOS (2025-08-28) ——
    # Marca si ya se hizo una toma de ganancias parcial
    partial_taken: Mapped[bool] = mapped_column(Boolean, default=False)

    # Campo explícito solicitado (además de peak_price_usd). Se mantiene separado.
    peak_price: Mapped[float] = mapped_column(Float, default=0.0)

    # —— relaciones ——
    token: Mapped["Token"] = relationship(back_populates="positions")

    # ────── ALIASES de compatibilidad (no columnas) ──────
    # qty_lamports <-> qty
    @property
    def qty_lamports(self) -> int:
        return int(getattr(self, "qty", 0) or 0)

    @qty_lamports.setter
    def qty_lamports(self, v: int) -> None:
        self.qty = int(max(0, v))

    # price_source_close <-> price_source_at_close
    @property
    def price_source_close(self) -> Optional[str]:
        return getattr(self, "price_source_at_close")

    @price_source_close.setter
    def price_source_close(self, v: Optional[str]) -> None:
        self.price_source_at_close = v

    # price_source (compra) <-> price_source_at_buy
    @property
    def price_source(self) -> Optional[str]:
        return getattr(self, "price_source_at_buy")

    @price_source.setter
    def price_source(self, v: Optional[str]) -> None:
        self.price_source_at_buy = v

    # liq_at_buy_usd (alias) <-> buy_liquidity_usd
    @property
    def liq_at_buy_usd(self) -> Optional[float]:
        return getattr(self, "buy_liquidity_usd")

    @liq_at_buy_usd.setter
    def liq_at_buy_usd(self, v: Optional[float]) -> None:
        self.buy_liquidity_usd = v if v is None else float(v)

    # peak_price (propiedad derivada) ↔ peak_price_usd
    # Nota: además existe la columna peak_price; esta propiedad ofrece un alias
    # “seguro” y mantiene sincronizadas ambas vistas.
    @property
    def peak_price_prop(self) -> float:
        return float(getattr(self, "peak_price_usd", 0.0) or 0.0)

    @peak_price_prop.setter
    def peak_price_prop(self, v: float) -> None:
        try:
            vv = float(v)
        except Exception:
            vv = 0.0
        self.peak_price_usd = vv
        # Mantén razonablemente sincronizado el campo explícito si lo usas:
        self.peak_price = vv

    # Azúcar sintáctico
    @property
    def is_open(self) -> bool:
        return not bool(self.closed)

    def __repr__(self) -> str:  # pragma: no cover
        alias = (self.token_mint or self.address or "")[:4]
        status = "OPEN" if not self.closed else "CLOSED"
        return f"<Position {self.symbol or alias} {status} opened={self.opened_at.isoformat()}>"



# ───────────────────────── RevivedToken ────────────────────────
class RevivedToken(Base):
    __tablename__ = "revived_tokens"
    __table_args__ = (
        Index("ix_revived_first_listed", "first_listed"),
        Index("ix_revived_revived_at", "revived_at"),
    )

    token_address: Mapped[str] = mapped_column(
        String(64), ForeignKey("tokens.address"), primary_key=True
    )
    first_listed: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True))
    revived_at:   Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True))

    liq_revived:  Mapped[float] = mapped_column(Float, default=0.0)
    vol_revived:  Mapped[float] = mapped_column(Float, default=0.0)
    buyers_delta: Mapped[int]   = mapped_column(Integer, default=0)

    token: Mapped["Token"] = relationship(back_populates="revived", uselist=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RevivedToken {self.token_address[:4]} revived_at={self.revived_at.isoformat()}>"
