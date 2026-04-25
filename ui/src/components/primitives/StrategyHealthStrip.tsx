import type { StrategyHealthEntry } from "../../lib/api";
import { formatDecimal, formatPct, formatTimestamp } from "../../lib/format";
import { StatusChip } from "./StatusChip";


interface StrategyHealthStripProps {
  items: Record<string, StrategyHealthEntry>;
  onSelect?: (regime: string, item: StrategyHealthEntry) => void;
}


function toneFromHealthState(value: string | null | undefined) {
  switch (value) {
    case "normal":
      return "success";
    case "cooldown":
      return "warn";
    case "disabled":
      return "danger";
    case "shadow":
      return "info";
    case "off":
      return "neutral";
    default:
      return "info";
  }
}


function rateToPct(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  const normalized = value <= 1 ? value * 100 : value;
  return formatPct(normalized);
}


export function StrategyHealthStrip({ items, onSelect }: StrategyHealthStripProps) {
  const regimes = Object.entries(items).sort(([left], [right]) => left.localeCompare(right));

  if (!regimes.length) {
    return <p className="empty-note">No strategy health snapshot available.</p>;
  }

  return (
    <div className="strategy-grid">
      {regimes.map(([regime, item]) => {
        const content = (
          <>
            <header className="strategy-card__header">
              <div>
                <p className="surface__eyebrow">{regime.replaceAll("_", " ")}</p>
                <h3 className="surface__title">{item.requested_mode || "unknown mode"}</h3>
              </div>
              <StatusChip
                compact
                label={item.health_state || "unknown"}
                mono
                tone={toneFromHealthState(item.health_state)}
              />
            </header>

            <div className="strategy-card__stats">
              <div>
                <span>Trades</span>
                <strong>{item.trade_count ?? 0}</strong>
              </div>
              <div>
                <span>Average PnL</span>
                <strong>{formatPct(item.avg_pnl_pct)}</strong>
              </div>
              <div>
                <span>Win rate</span>
                <strong>{rateToPct(item.win_rate)}</strong>
              </div>
              <div>
                <span>Execution</span>
                <strong>{rateToPct(item.exec_rate)}</strong>
              </div>
              <div>
                <span>Pricing</span>
                <strong>{rateToPct(item.price_rate)}</strong>
              </div>
              <div>
                <span>Loss streak</span>
                <strong>{formatDecimal(item.consecutive_losses)}</strong>
              </div>
            </div>

            <footer className="strategy-card__footer">
              <span>{item.disable_reason || "No disable reason"}</span>
              <small>{item.cooldown_until ? `cooldown until ${formatTimestamp(item.cooldown_until)}` : "no cooldown timer"}</small>
            </footer>
          </>
        );

        if (!onSelect) {
          return (
            <article className="strategy-card" key={regime}>
              {content}
            </article>
          );
        }

        return (
          <button className="strategy-card strategy-card--interactive" key={regime} onClick={() => onSelect(regime, item)} type="button">
            {content}
          </button>
        );
      })}
    </div>
  );
}
