import type { RuntimeEventItem } from "../../lib/api";
import { formatRelative, formatTimestamp } from "../../lib/format";
import { StatusChip } from "./StatusChip";


interface TimelineRailProps {
  items: RuntimeEventItem[];
  emptyMessage?: string;
  onSelect?: (item: RuntimeEventItem) => void;
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


function payloadPreview(payload: Record<string, unknown>) {
  const pairs = Object.entries(payload).filter(([, value]) => value !== null && value !== undefined);
  return pairs.slice(0, 4);
}


export function TimelineRail({ items, emptyMessage = "No events available.", onSelect }: TimelineRailProps) {
  if (!items.length) {
    return <p className="empty-note">{emptyMessage}</p>;
  }

  return (
    <ol className="timeline-rail">
      {items.map((item) => {
        const preview = payloadPreview(item.payload);
        const content = (
          <>
            <div className="timeline-rail__head">
              <div className="timeline-rail__meta">
                <StatusChip compact label={item.event_type} mono tone={eventTone(item.event_type)} />
                <span>{item.summary}</span>
              </div>
              <time dateTime={item.ts_utc} title={formatTimestamp(item.ts_utc)}>
                {formatRelative(item.ts_utc)} ago
              </time>
            </div>
            <div className="timeline-rail__body">
              <p>{item.address || "global event"}</p>
              {preview.length ? (
                <div className="timeline-rail__payload">
                  {preview.map(([key, value]) => (
                    <span className="timeline-pill" key={`${item.id}-${key}`}>
                      <strong>{key}</strong>
                      <em>{String(value)}</em>
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          </>
        );

        if (!onSelect) {
          return (
            <li className="timeline-rail__item" key={item.id}>
              {content}
            </li>
          );
        }

        return (
          <li className="timeline-rail__item" key={item.id}>
            <button className="timeline-rail__button" onClick={() => onSelect(item)} type="button">
              {content}
            </button>
          </li>
        );
      })}
    </ol>
  );
}
