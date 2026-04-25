import type { ReactNode } from "react";


interface ChartShellProps {
  title: string;
  subtitle: string;
  caption: string;
  children?: ReactNode;
  className?: string;
}


export function ChartShell({ title, subtitle, caption, children, className }: ChartShellProps) {
  return (
    <section className={["chart-shell", className].filter(Boolean).join(" ")}>
      <header className="chart-shell__header">
        <div>
          <p className="surface__eyebrow">Primary visual slot</p>
          <h3 className="surface__title">{title}</h3>
          <p className="surface__subtitle">{subtitle}</p>
        </div>
      </header>
      {children ? (
        <div className="chart-shell__content">{children}</div>
      ) : (
        <div className="chart-shell__placeholder" aria-hidden="true">
          {Array.from({ length: 12 }, (_, index) => (
            <span
              key={index}
              style={{
                height: `${32 + ((index * 11) % 44)}%`,
              }}
            />
          ))}
        </div>
      )}
      <footer className="chart-shell__footer">{caption}</footer>
    </section>
  );
}
