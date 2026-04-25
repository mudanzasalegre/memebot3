import { useDrawer } from "../../app/drawer";


export function ContextDrawer() {
  const { panel, closePanel } = useDrawer();

  return (
    <div className={["context-drawer", panel ? "context-drawer--open" : ""].filter(Boolean).join(" ")}>
      <button
        aria-hidden={panel ? undefined : true}
        className="context-drawer__scrim"
        onClick={closePanel}
        tabIndex={panel ? 0 : -1}
        type="button"
      />
      <aside aria-hidden={!panel} className="context-drawer__panel">
        {panel ? (
          <>
            <header className="context-drawer__header">
              <div>
                {panel.eyebrow ? <p className="surface__eyebrow">{panel.eyebrow}</p> : null}
                <h2 className="surface__title">{panel.title}</h2>
                {panel.description ? <p className="surface__subtitle">{panel.description}</p> : null}
              </div>
              <button className="ui-button ui-button--ghost" onClick={closePanel} type="button">
                Close
              </button>
            </header>
            <div className="context-drawer__body">{panel.content}</div>
          </>
        ) : null}
      </aside>
    </div>
  );
}
