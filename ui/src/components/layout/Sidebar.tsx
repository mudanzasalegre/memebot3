import { NavLink } from "react-router-dom";

import { navGroups, routeCatalog } from "../../data/routes";


export function Sidebar() {
  return (
    <aside className="sidebar">
      <div className="sidebar__brand">
        <p>MemeBot 3</p>
        <h1>Ops Desk</h1>
        <span>Industrial editorial shell for monitoring, inspection, and control.</span>
      </div>

      <nav className="sidebar__nav" aria-label="Primary navigation">
        {navGroups.map((group) => (
          <section className="sidebar__group" key={group}>
            <p className="sidebar__group-label">{group}</p>
            <div className="sidebar__links">
              {routeCatalog
                .filter((route) => route.group === group && route.inNav !== false)
                .map((route) => (
                  <NavLink
                    className={({ isActive }) =>
                      ["sidebar__link", isActive ? "sidebar__link--active" : ""].filter(Boolean).join(" ")
                    }
                    key={route.id}
                    to={route.path}
                  >
                    <span>{route.navLabel}</span>
                    <small>{route.phase}</small>
                  </NavLink>
                ))}
            </div>
          </section>
        ))}
      </nav>

      <div className="sidebar__footer">
        <p>Shell first.</p>
        <span>PR-UI-15 adds local auth, role gating, and saved views over the live operator shell.</span>
      </div>
    </aside>
  );
}
