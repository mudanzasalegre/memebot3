import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { Banner } from "../components/primitives/Banner";
import { StatusChip } from "../components/primitives/StatusChip";


function resolveNextPath(searchParams: URLSearchParams) {
  const next = searchParams.get("next");
  if (!next || !next.startsWith("/")) {
    return "/overview";
  }
  return next;
}


export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { session, isAuthenticated, isLoading, error, login } = useAuth();
  const [username, setUsername] = useState("operator");
  const [password, setPassword] = useState("operator");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const nextPath = resolveNextPath(searchParams);

  useEffect(() => {
    if (isAuthenticated) {
      navigate(nextPath, { replace: true });
    }
  }, [isAuthenticated, navigate, nextPath]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSubmitting(true);
    const ok = await login(username.trim(), password);
    setIsSubmitting(false);
    if (ok) {
      navigate(nextPath, { replace: true });
    }
  }

  function useLocalUser(nextUsername: string) {
    setUsername(nextUsername);
    if (session?.default_credentials_active) {
      setPassword(nextUsername);
    }
  }

  return (
    <div className="auth-screen">
      <section className="auth-panel">
        <div className="auth-panel__header">
          <div>
            <p className="surface__eyebrow">Operate / local access</p>
            <h1>UI login</h1>
            <p>
              The shell now requires a local session before it mounts runtime, trading, analytics, and control pages.
            </p>
          </div>
          <div className="page-hero__meta">
            <StatusChip label={session?.auth_mode || "local"} tone="info" compact mono />
            {session?.default_credentials_active ? <StatusChip label="default credentials active" tone="warn" compact /> : null}
          </div>
        </div>

        {isLoading && !error && !session ? (
          <Banner
            detail="Waiting for the local API session bootstrap. If you just ran `start_stack.ps1`, this should clear automatically in a few seconds."
            title="Connecting to API"
            tone="info"
          />
        ) : null}

        {error ? <Banner detail={error} title="Login failed" tone="danger" /> : null}

        {session?.default_credentials_active ? (
          <Banner
            detail="Until you override `UI_LOCAL_USERS`, the default local accounts are `viewer/viewer`, `operator/operator`, and `admin/admin`."
            title="Default local credentials"
            tone="warn"
          />
        ) : null}

        <form className="auth-form" onSubmit={submit}>
          <label className="filter-field">
            <span>Username</span>
            <input
              autoComplete="username"
              className="ui-field"
              onChange={(event) => setUsername(event.target.value)}
              placeholder="operator"
              type="text"
              value={username}
            />
          </label>

          <label className="filter-field">
            <span>Password</span>
            <input
              autoComplete="current-password"
              className="ui-field"
              onChange={(event) => setPassword(event.target.value)}
              placeholder="password"
              type="password"
              value={password}
            />
          </label>

          <button className="ui-button ui-button--primary" disabled={isLoading || isSubmitting} type="submit">
            {isSubmitting ? "Signing in..." : "Sign in"}
          </button>
        </form>

        {session?.available_users?.length ? (
          <div className="auth-quick-users">
            <div className="surface__header">
              <div>
                <p className="surface__eyebrow">Configured local users</p>
                <h2 className="surface__title">Quick fill</h2>
                <p className="surface__subtitle">These accounts come from `UI_LOCAL_USERS` or the local defaults.</p>
              </div>
            </div>
            <div className="command-grid">
              {session.available_users.map((user) => (
                <article className="command-card" key={user.username}>
                  <div className="command-card__header">
                    <div>
                      <p className="surface__eyebrow">{user.role}</p>
                      <h3>{user.display_name}</h3>
                    </div>
                    <StatusChip compact label={user.username} tone="neutral" mono />
                  </div>
                  <p>Use this local role to open the shell with the permission set mapped in `PR-UI-15`.</p>
                  <div className="command-card__footer">
                    <small>{user.role === "viewer" ? "Read-only operator posture." : "Can access a wider operational surface."}</small>
                    <button className="ui-button ui-button--ghost" onClick={() => useLocalUser(user.username)} type="button">
                      Use {user.username}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
