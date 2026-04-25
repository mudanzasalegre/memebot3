import type { HTMLAttributes, ReactNode } from "react";


interface SurfaceProps extends HTMLAttributes<HTMLElement> {
  as?: "section" | "article" | "aside";
  eyebrow?: string;
  title?: string;
  subtitle?: string;
  actions?: ReactNode;
}


export function Surface({
  as = "section",
  eyebrow,
  title,
  subtitle,
  actions,
  className,
  children,
  ...rest
}: SurfaceProps) {
  const Component = as;

  return (
    <Component className={["surface", className].filter(Boolean).join(" ")} {...rest}>
      {(eyebrow || title || subtitle || actions) && (
        <header className="surface__header">
          <div>
            {eyebrow ? <p className="surface__eyebrow">{eyebrow}</p> : null}
            {title ? <h2 className="surface__title">{title}</h2> : null}
            {subtitle ? <p className="surface__subtitle">{subtitle}</p> : null}
          </div>
          {actions ? <div className="surface__actions">{actions}</div> : null}
        </header>
      )}
      <div className="surface__body">{children}</div>
    </Component>
  );
}
