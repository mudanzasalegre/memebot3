import {
  createContext,
  startTransition,
  useContext,
  useEffect,
  useEffectEvent,
  useState,
  type ReactNode,
} from "react";

import {
  fetchEnvelope,
  postEnvelope,
  type AuthSessionData,
} from "../lib/api";

const SESSION_BOOTSTRAP_MAX_RETRIES = 15;
const SESSION_BOOTSTRAP_RETRY_DELAY_MS = 1000;

function describeAuthError(nextError: unknown) {
  return nextError instanceof Error ? nextError.message : "Unable to load auth session";
}

function isTransientSessionBootstrapError(nextError: unknown) {
  if (!(nextError instanceof Error)) {
    return false;
  }

  const message = nextError.message.toLowerCase();
  return (
    message.includes("http 502") ||
    message.includes("http 503") ||
    message.includes("http 504") ||
    message.includes("failed to fetch") ||
    message.includes("network error") ||
    message.includes("networkerror")
  );
}

function waitForRetryWindow(delayMs: number, signal?: AbortSignal) {
  return new Promise<boolean>((resolve) => {
    if (signal?.aborted) {
      resolve(false);
      return;
    }

    const timeoutId = window.setTimeout(() => {
      signal?.removeEventListener("abort", handleAbort);
      resolve(!signal?.aborted);
    }, delayMs);

    function handleAbort() {
      window.clearTimeout(timeoutId);
      signal?.removeEventListener("abort", handleAbort);
      resolve(false);
    }

    signal?.addEventListener("abort", handleAbort, { once: true });
  });
}


interface AuthContextValue {
  session: AuthSessionData | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  hasPermission: (permission: string) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);


export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSessionData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadSession = useEffectEvent(async (signal?: AbortSignal, retryOnStartup = false) => {
    setIsLoading(true);
    setError(null);

    for (let attempt = 0; attempt <= SESSION_BOOTSTRAP_MAX_RETRIES; attempt += 1) {
      try {
        const envelope = await fetchEnvelope<AuthSessionData>("/api/v1/auth/session", signal);
        if (signal?.aborted) {
          return;
        }
        startTransition(() => {
          setSession(envelope.data);
          setError(null);
          setIsLoading(false);
        });
        return;
      } catch (nextError) {
        if (signal?.aborted) {
          return;
        }

        const canRetry =
          retryOnStartup &&
          attempt < SESSION_BOOTSTRAP_MAX_RETRIES &&
          isTransientSessionBootstrapError(nextError);

        if (canRetry) {
          const shouldContinue = await waitForRetryWindow(SESSION_BOOTSTRAP_RETRY_DELAY_MS, signal);
          if (!shouldContinue) {
            return;
          }
          continue;
        }

        setError(describeAuthError(nextError));
        setIsLoading(false);
        return;
      }
    }
  });

  useEffect(() => {
    const controller = new AbortController();
    void loadSession(controller.signal, true);
    return () => controller.abort();
  }, []);

  async function login(username: string, password: string) {
    setError(null);
    setIsLoading(true);
    try {
      const envelope = await postEnvelope<AuthSessionData, { username: string; password: string }>(
        "/api/v1/auth/login",
        { username, password },
      );
      startTransition(() => {
        setSession(envelope.data);
        setError(null);
        setIsLoading(false);
      });
      return true;
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Unable to login");
      setIsLoading(false);
      return false;
    }
  }

  async function logout() {
    setError(null);
    setIsLoading(true);
    try {
      const envelope = await postEnvelope<AuthSessionData, Record<string, never>>("/api/v1/auth/logout", {});
      startTransition(() => {
        setSession(envelope.data);
        setError(null);
        setIsLoading(false);
      });
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Unable to logout");
      setIsLoading(false);
    }
  }

  async function refresh() {
    await loadSession(undefined, false);
  }

  const value: AuthContextValue = {
    session,
    isAuthenticated: Boolean(session?.is_authenticated && session.user),
    isLoading,
    error,
    login,
    logout,
    refresh,
    hasPermission(permission) {
      return Boolean(session?.user?.permissions.includes(permission));
    },
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}


export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
