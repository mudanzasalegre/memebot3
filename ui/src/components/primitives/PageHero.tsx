import type { ReactNode } from "react";


interface PageHeroProps {
  eyebrow: string;
  title: string;
  summary: string;
  question: string;
  meta?: ReactNode;
  actions?: ReactNode;
}


export function PageHero({ eyebrow, title, summary, question, meta, actions }: PageHeroProps) {
  return (
    <section className="page-hero" data-reveal="hero">
      <div className="page-hero__copy">
        <p className="page-hero__eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="page-hero__summary">{summary}</p>
      </div>
      <div className="page-hero__question">
        <span>Operator question</span>
        <strong>{question}</strong>
      </div>
      {meta ? <div className="page-hero__meta">{meta}</div> : null}
      {actions ? <div className="page-hero__actions">{actions}</div> : null}
    </section>
  );
}
