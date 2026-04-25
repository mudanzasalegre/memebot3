import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { AppShell } from "../components/layout/AppShell";


export function ProtectedLayout() {
  const location = useLocation();
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="auth-screen">
        <div className="auth-panel">
          <p className="surface__eyebrow">UI session</p>
          <h1>Loading session</h1>
          <p>The shell is validating the local operator session before mounting live runtime pages.</p>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) {
    const next = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate replace to={`/login?next=${encodeURIComponent(next)}`} />;
  }

  return <AppShell />;
}
