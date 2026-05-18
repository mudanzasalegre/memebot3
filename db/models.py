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
    Text,
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
    dex_id: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
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

    # Cantidad remanente en lamports del token (no SOL)
    qty:     Mapped[int] = mapped_column(Integer)
    entry_qty: Mapped[int] = mapped_column(Integer, default=0)

    # —— entrada (compra) ——
    buy_price_usd: Mapped[float] = mapped_column(Float)
    price_source_at_buy: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    buy_tx_sig: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)  # firma de compra (Solana ~88 chars)
    entry_regime: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    size_bucket: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    size_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    buy_amount_sol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_notional_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_ai_proba: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_score_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    entry_lane: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entry_subprofile: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    entry_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gate_profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    strategy_version: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    experiment_id: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    exit_profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    config_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    buy_dex_id: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    buy_price_pct_5m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    buy_txns_last_5m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    buy_liquidity_is_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    mcap_bucket: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    price5m_bucket: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

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
    max_pnl_pct_seen: Mapped[float] = mapped_column(Float, default=0.0)
    realized_qty: Mapped[int] = mapped_column(Integer, default=0)
    realized_proceeds_usd: Mapped[float] = mapped_column(Float, default=0.0)
    realized_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    effective_exit_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    runner_exit_profile: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    time_to_partial_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_to_peak_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    peak_after_partial_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_from_peak_giveback_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # —— NUEVOS CAMPOS (2025-08-28) ——
    # Marca si ya se hizo una toma de ganancias parcial
    partial_taken: Mapped[bool] = mapped_column(Boolean, default=False)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_ladder_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_partial_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_partial_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_partial_qty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_partial_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

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


class BotRuntimeState(Base):
    __tablename__ = "bot_runtime_state"

    bot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    updated_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    heartbeat_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    started_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    process_state: Mapped[str] = mapped_column(String(16), default="starting")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    discovery_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    buys_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    retrain_state: Mapped[str] = mapped_column(String(16), default="idle")
    reports_refresh_state: Mapped[str] = mapped_column(String(16), default="idle")
    wallet_sol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    wallet_checked_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    open_positions_count: Mapped[int] = mapped_column(Integer, default=0)
    queue_pending: Mapped[int] = mapped_column(Integer, default=0)
    queue_requeued: Mapped[int] = mapped_column(Integer, default=0)
    queue_cooldown: Mapped[int] = mapped_column(Integer, default=0)
    queue_oldest_first_seen_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    buy_limiter_in_window: Mapped[int] = mapped_column(Integer, default=0)
    buy_limiter_window_s: Mapped[int] = mapped_column(Integer, default=0)
    discovery_last_ok_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    monitor_last_ok_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    ml_gate_json: Mapped[str] = mapped_column(Text, default="{}")
    strategy_health_json: Mapped[str] = mapped_column(Text, default="{}")
    research_json: Mapped[str] = mapped_column(Text, default="{}")
    queue_items_json: Mapped[str] = mapped_column(Text, default="{}")
    build_info_json: Mapped[str] = mapped_column(Text, default="{}")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BotRuntimeState {self.bot_id} state={self.process_state} updated={self.updated_at.isoformat()}>"


class ControlCommand(Base):
    __tablename__ = "control_commands"
    __table_args__ = (
        Index("ix_control_commands_bot_status_requested", "bot_id", "status", "requested_at"),
        Index("ix_control_commands_bot_command_requested", "bot_id", "command_type", "requested_at"),
        Index("ux_control_commands_bot_idempotency", "bot_id", "idempotency_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[str] = mapped_column(String(32), default="main", index=True)
    command_type: Mapped[str] = mapped_column(String(32), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    requested_from: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    requested_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    started_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ControlCommand id={self.id} bot={self.bot_id} type={self.command_type} status={self.status}>"


class UiSavedView(Base):
    __tablename__ = "ui_saved_views"
    __table_args__ = (
        Index("ix_ui_saved_views_page_owner_updated", "page_key", "created_by", "updated_at"),
        Index("ix_ui_saved_views_owner_updated", "created_by", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    page_key: Mapped[str] = mapped_column(String(64), index=True)
    view_name: Mapped[str] = mapped_column(String(128))
    filters_json: Mapped[str] = mapped_column(Text, default="{}")
    layout_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UiSavedView id={self.id} page={self.page_key} owner={self.created_by} name={self.view_name}>"
