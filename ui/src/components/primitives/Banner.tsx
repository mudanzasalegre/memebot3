import type { ReactNode } from "react";

import { StatusChip } from "./StatusChip";


interface BannerProps {
  tone: "info" | "warn" | "danger" | "success";
  title: string;
  detail: string;
  actions?: ReactNode;
}


export function Banner({ tone, title, detail, actions }: BannerProps) {
  return (
    <aside className={`banner banner--${tone}`}>
      <div className="banner__copy">
        <StatusChip label={title} tone={tone} compact />
        <p>{detail}</p>
      </div>
      {actions ? <div className="banner__actions">{actions}</div> : null}
    </aside>
  );
}
