import type { RuntimeEventItem } from "../../lib/api";
import { humanizeKey } from "../../lib/format";
import { StatusChip } from "./StatusChip";


interface RuntimePulseProps {
  items: RuntimeEventItem[];
}


function eventTone(eventType: string) {
  if (/(error|fail|reject|panic)/i.test(eventType)) {
    return "danger";
  }
  if (/(cooldown|pause|wait)/i.test(eventType)) {
    return "warn";
  }
  if (/(buy|sell|execution)/i.test(eventType)) {
    return "success";
  }
  return "info";
}


export function RuntimePulse({ items }: RuntimePulseProps) {
  if (!items.length) {
    return <p className="empty-note">No recent runtime events available.</p>;
  }

  const counts = new Map<string, number>();
  for (const item of items) {
    counts.set(item.event_type, (counts.get(item.event_type) || 0) + 1);
  }

  const lanes = [...counts.entries()].sort((left, right) => right[1] - left[1]).slice(0, 6);
  const max = Math.max(...lanes.map(([, count]) => count));

  return (
    <div className="pulse-panel">
      <div className="pulse-panel__summary">
        {lanes.map(([eventType, count]) => (
          <div className="pulse-panel__lane" key={eventType}>
            <div className="pulse-panel__label">
              <StatusChip compact label={eventType} mono tone={eventTone(eventType)} />
              <strong>{count}</strong>
            </div>
            <div className="pulse-panel__bar">
              <span
                style={{
                  width: `${Math.max((count / max) * 100, 8)}%`,
                }}
              />
            </div>
          </div>
        ))}
      </div>

      <div className="pulse-panel__ticks">
        {items.slice(0, 12).map((item) => (
          <div className={`pulse-panel__tick pulse-panel__tick--${eventTone(item.event_type)}`} key={item.id} title={`${humanizeKey(item.event_type)} · ${item.summary}`}>
            <span />
          </div>
        ))}
      </div>
    </div>
  );
}
