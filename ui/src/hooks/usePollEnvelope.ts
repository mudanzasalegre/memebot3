import { useEffect, useEffectEvent, useState } from "react";

import { fetchEnvelope, type Envelope } from "../lib/api";


interface PollEnvelopeState<T> {
  envelope: Envelope<T> | null;
  isLoading: boolean;
  error: string | null;
  refreshedAt: string | null;
}


export function usePollEnvelope<T>(path: string, intervalMs: number) {
  const [state, setState] = useState<PollEnvelopeState<T>>({
    envelope: null,
    isLoading: true,
    error: null,
    refreshedAt: null,
  });

  const runPoll = useEffectEvent(async (signal?: AbortSignal) => {
    try {
      const envelope = await fetchEnvelope<T>(path, signal);
      setState({
        envelope,
        isLoading: false,
        error: null,
        refreshedAt: new Date().toISOString(),
      });
    } catch (error) {
      if (signal?.aborted) {
        return;
      }
      setState((current) => ({
        ...current,
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown request failure",
      }));
    }
  });

  useEffect(() => {
    const initialController = new AbortController();
    void runPoll(initialController.signal);

    const timer = window.setInterval(() => {
      const controller = new AbortController();
      void runPoll(controller.signal);
    }, intervalMs);

    return () => {
      initialController.abort();
      window.clearInterval(timer);
    };
  }, [intervalMs, path]);

  return {
    ...state,
    refetch() {
      void runPoll();
    },
  };
}
