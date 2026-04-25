import { useState } from "react";

import { useAuth } from "../../auth/AuthProvider";
import { usePollEnvelope } from "../../hooks/usePollEnvelope";
import {
  deleteEnvelope,
  patchEnvelope,
  postEnvelope,
  type SavedViewCreateRequest,
  type SavedViewDeleteData,
  type SavedViewItem,
  type SavedViewsData,
  type SavedViewUpdateRequest,
} from "../../lib/api";
import { formatRelative, formatTimestamp } from "../../lib/format";
import { StatusChip } from "./StatusChip";


interface SavedViewsToolbarProps<T extends Record<string, unknown>> {
  pageKey: string;
  currentFilters: T;
  onApply: (filters: T) => void;
  layout?: Record<string, unknown>;
}


function buildPath(pathname: string, params: Record<string, string | boolean | null | undefined>) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "") {
      return;
    }
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `${pathname}?${query}` : pathname;
}


export function SavedViewsToolbar<T extends Record<string, unknown>>({
  pageKey,
  currentFilters,
  onApply,
  layout = {},
}: SavedViewsToolbarProps<T>) {
  const { session } = useAuth();
  const [viewName, setViewName] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; message: string } | null>(null);
  const viewsQuery = usePollEnvelope<SavedViewsData>(
    buildPath("/api/v1/saved-views", { page_key: pageKey, mine: true }),
    15000,
  );

  const items = viewsQuery.envelope?.data.items || [];
  const selectedView = items.find((item) => item.id === selectedId) || null;

  async function saveCurrent() {
    const trimmed = viewName.trim();
    if (!trimmed) {
      setFeedback({ tone: "danger", message: "View name is required." });
      return;
    }
    setIsSubmitting(true);
    setFeedback(null);
    try {
      const envelope = await postEnvelope<SavedViewItem, SavedViewCreateRequest>("/api/v1/saved-views", {
        page_key: pageKey,
        view_name: trimmed,
        filters: currentFilters,
        layout,
      });
      setSelectedId(envelope.data.id);
      setViewName("");
      setFeedback({ tone: "success", message: `Saved view ${envelope.data.view_name}.` });
      viewsQuery.refetch();
    } catch (error) {
      setFeedback({ tone: "danger", message: error instanceof Error ? error.message : "Unable to save view" });
    } finally {
      setIsSubmitting(false);
    }
  }

  async function updateSelected() {
    if (!selectedView) {
      setFeedback({ tone: "danger", message: "Select a saved view first." });
      return;
    }
    setIsSubmitting(true);
    setFeedback(null);
    try {
      const envelope = await patchEnvelope<SavedViewItem, SavedViewUpdateRequest>(`/api/v1/saved-views/${selectedView.id}`, {
        view_name: viewName.trim() || selectedView.view_name,
        filters: currentFilters,
        layout,
      });
      setViewName("");
      setFeedback({ tone: "success", message: `Updated view ${envelope.data.view_name}.` });
      viewsQuery.refetch();
    } catch (error) {
      setFeedback({ tone: "danger", message: error instanceof Error ? error.message : "Unable to update view" });
    } finally {
      setIsSubmitting(false);
    }
  }

  async function deleteSelected() {
    if (!selectedView) {
      setFeedback({ tone: "danger", message: "Select a saved view first." });
      return;
    }
    setIsSubmitting(true);
    setFeedback(null);
    try {
      await deleteEnvelope<SavedViewDeleteData>(`/api/v1/saved-views/${selectedView.id}`);
      setSelectedId(null);
      setViewName("");
      setFeedback({ tone: "success", message: `Deleted view ${selectedView.view_name}.` });
      viewsQuery.refetch();
    } catch (error) {
      setFeedback({ tone: "danger", message: error instanceof Error ? error.message : "Unable to delete view" });
    } finally {
      setIsSubmitting(false);
    }
  }

  function applyView(item: SavedViewItem) {
    setSelectedId(item.id);
    setViewName(item.view_name);
    onApply(item.filters as T);
    setFeedback(null);
  }

  return (
    <div className="saved-views">
      <div className="saved-views__header">
        <div>
          <p className="surface__eyebrow">Saved views</p>
          <h3>Reusable page presets</h3>
        </div>
        <div className="page-hero__meta">
          <StatusChip label={pageKey} tone="neutral" compact mono />
          {session?.user ? <StatusChip label={session.user.username} tone="info" compact mono /> : null}
          <StatusChip label={`${items.length} views`} tone="neutral" compact />
        </div>
      </div>

      <div className="saved-views__composer">
        <label className="filter-field">
          <span>View name</span>
          <input
            className="ui-field"
            onChange={(event) => setViewName(event.target.value)}
            placeholder={`save ${pageKey} filter set`}
            type="text"
            value={viewName}
          />
        </label>

        <div className="saved-views__actions">
          <button className="ui-button ui-button--ghost" disabled={isSubmitting} onClick={() => void saveCurrent()} type="button">
            Save current
          </button>
          <button
            className="ui-button ui-button--ghost"
            disabled={isSubmitting || !selectedView}
            onClick={() => void updateSelected()}
            type="button"
          >
            Update selected
          </button>
          <button
            className="ui-button ui-button--ghost"
            disabled={isSubmitting || !selectedView}
            onClick={() => void deleteSelected()}
            type="button"
          >
            Delete selected
          </button>
        </div>
      </div>

      {feedback ? <p className={`saved-views__feedback saved-views__feedback--${feedback.tone}`}>{feedback.message}</p> : null}

      <div className="saved-views__list">
        {items.map((item) => (
          <button
            className={["saved-view-chip", selectedId === item.id ? "saved-view-chip--active" : ""].filter(Boolean).join(" ")}
            key={item.id}
            onClick={() => applyView(item)}
            type="button"
          >
            <strong>{item.view_name}</strong>
            <span>{formatRelative(item.updated_at)} ago</span>
            <small title={formatTimestamp(item.updated_at)}>updated {formatTimestamp(item.updated_at)}</small>
          </button>
        ))}
        {!items.length ? <p className="empty-note">No saved views yet for this page.</p> : null}
      </div>
    </div>
  );
}
