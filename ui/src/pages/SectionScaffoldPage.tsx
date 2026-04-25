import { useOutletContext, useParams } from "react-router-dom";

import { useDrawer } from "../app/drawer";
import type { ShellOutletContext } from "../components/layout/AppShell";
import { Banner } from "../components/primitives/Banner";
import { ChartShell } from "../components/primitives/ChartShell";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { StatusChip, toneFromStatus } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { getRouteById, type EndpointDescriptor } from "../data/routes";
import { humanizeKey } from "../lib/format";


interface SectionScaffoldPageProps {
  routeId: string;
}


const endpointColumns: DataColumn<EndpointDescriptor>[] = [
  {
    id: "path",
    header: "Endpoint",
    mono: true,
    render: (row) => row.path,
  },
  {
    id: "purpose",
    header: "Purpose",
    render: (row) => row.purpose,
  },
  {
    id: "cadence",
    align: "right",
    header: "Refresh",
    render: (row) => row.cadence,
  },
];


export function SectionScaffoldPage({ routeId }: SectionScaffoldPageProps) {
  const route = getRouteById(routeId);
  const { openPanel } = useDrawer();
  const { tradeId } = useParams();
  const { overviewEnvelope, sourcesEnvelope, timeRange } = useOutletContext<ShellOutletContext>();

  if (!route) {
    return (
      <Surface eyebrow="Routing error" title="Route not found">
        <p>The requested scaffold route does not exist in the route catalog.</p>
      </Surface>
    );
  }

  const liveSources = sourcesEnvelope?.data.sources || [];
  const visibleTitle = routeId === "tradeReplay" && tradeId ? `${route.title} #${tradeId}` : route.title;

  return (
    <div className="page-stack">
      <PageHero
        actions={
          <button
            className="ui-button ui-button--ghost"
            onClick={() =>
              openPanel({
                eyebrow: `${route.group} / implementation brief`,
                title: route.title,
                description: `This page is already routed and visually prepared in PR-UI-8. Domain widgets land in ${route.phase}.`,
                content: (
                  <div className="drawer-stack">
                    {route.panels.map((panel) => (
                      <div className="drawer-kv" key={panel}>
                        <strong>{panel}</strong>
                        <span>Reserved in shell and design system.</span>
                      </div>
                    ))}
                  </div>
                ),
              })
            }
            type="button"
          >
            Open route brief
          </button>
        }
        eyebrow={route.eyebrow}
        meta={
          <>
            <StatusChip label={route.phase} tone="info" />
            <StatusChip
              label={overviewEnvelope?.meta.degraded ? "shell degraded" : "shell ready"}
              tone={overviewEnvelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={`range ${timeRange}`} tone="neutral" compact />
          </>
        }
        question={route.question}
        summary={route.summary}
        title={visibleTitle}
      />

      <Banner
        detail={`Routing, sidebar, topbar, drawer, table kit, chart shell, theme tokens, and motion tokens are already in place. ${route.phase} can now focus on domain content instead of shell work.`}
        title="Shell foundation ready"
        tone="info"
      />

      <div className="editorial-grid">
        <Surface eyebrow="Planned panels" title="What this page will own next">
          <div className="stack-list">
            {route.panels.map((panel) => (
              <div className="stack-list__item" key={panel}>
                <span>{panel}</span>
                <StatusChip compact label="reserved" tone="info" />
              </div>
            ))}
          </div>
        </Surface>

        <ChartShell
          caption={`Time range preset: ${timeRange}`}
          subtitle="Charts stay subordinate to the operator question. The shell already reserves a primary visual slot with the right density and framing."
          title={`${route.navLabel} visual slot`}
        />

        <Surface eyebrow="Endpoint contract" title="Live API footprint">
          <DataTable
            columns={endpointColumns}
            rowKey={(row) => `${row.path}-${row.purpose}`}
            rows={route.endpoints}
          />
        </Surface>

        <Surface eyebrow="Live shell signals" title="Shared health and source truth">
          <div className="stack-list">
            {liveSources.slice(0, 5).map((source) => (
              <div className="stack-list__item" key={source.source_key}>
                <span>{humanizeKey(source.source_key)}</span>
                <StatusChip compact label={source.status} tone={toneFromStatus(source.status)} mono />
              </div>
            ))}
          </div>
        </Surface>
      </div>
    </div>
  );
}
