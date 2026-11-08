import { useEffect, useMemo, useState } from "react";
import { Button, FluentProvider, webLightTheme } from "@fluentui/react-components";
import { PortalApi } from "./api";
import { Assistant } from "./Assistant";
import { FinOpsPage, OperationsPage } from "./Dashboards";
import { LoadState, Notice, useLoad } from "./components";
import type { Portal, Session } from "./types";

type Page = "assistant" | "finops" | "operations";
const pages = {
  assistant: { name: "Gateway assistant", subtitle: "Understand, troubleshoot and manage your AI gateway." },
  finops: { name: "FinOps", subtitle: "Turn gateway usage into transparent, attributable model economics." },
  operations: { name: "Operations", subtitle: "Understand request health, performance and governance signals." },
};
const theme = { ...webLightTheme, colorBrandBackground: "#126d80", colorBrandBackgroundHover: "#0c5363", colorBrandForeground1: "#126d80" };

export default function App() {
  const auth = useMemo(() => new PortalApi(), []);
  const session = useLoad((signal) => auth.session(signal), [auth]);
  return <FluentProvider theme={theme}>
    {session.loading || session.error ? <main className="welcome"><LoadState {...session} retry={session.refresh} /></main> :
      !session.value?.authenticated ? <main className="welcome"><div className="brand-mark">C</div><span className="eyebrow">CITADEL CONTROL PLANE</span>
        <h1>Your AI gateway.<br />Clarity, control, confidence.</h1>
        <p>A workspace for platform teams to manage access, understand model spend and keep AI workloads running.</p>
        <a className="sign-in" href="/auth/login">Sign in with Microsoft Entra</a><small>Access is governed by your tenant, application roles and hub grants.</small>
      </main> : <Authenticated session={session.value} logout={session.refresh} />}
  </FluentProvider>;
}

function Authenticated({ session, logout }: { session: Session; logout: () => void }) {
  const api = useMemo(() => new PortalApi(session.csrfToken), [session.csrfToken]);
  const portal = useLoad((signal) => api.portal(signal), [api]);
  const [error, setError] = useState("");
  const [page, setPage] = useState<Page>(() => currentPage());
  const [investigationPrompt, setInvestigationPrompt] = useState<string>();
  useEffect(() => {
    const update = () => setPage(currentPage());
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  function navigate(next: Page) {
    window.history.pushState(null, "", `/${next}`);
    setPage(next);
  }
  async function signOut() {
    try { await api.logout(); logout(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Sign-out failed."); }
  }
  return <div className="app-shell">
    <a className="skip-link" href="#main">Skip to main content</a>
    <aside className="sidebar"><a className="brand" href="/"><span className="brand-mark">C</span><span>CITADEL<small>AI gateway control plane</small></span></a>
      <span className="nav-label">WORKSPACE</span><nav aria-label="Primary navigation">
        {(Object.keys(pages) as Page[]).map((key, index) => <a href={`/${key}`} key={key} aria-current={page === key ? "page" : undefined}
          onClick={(event) => { if (!event.metaKey && !event.ctrlKey) { event.preventDefault(); navigate(key); } }}>
          <span className="nav-icon">{String(index + 1).padStart(2, "0")}</span>{pages[key].name}</a>)}
      </nav>
      <div className="sidebar-footer"><span className="connection-dot" />Single-tenant workspace<small>Private agent service</small></div>
    </aside>
    <div className="workspace"><header className="topbar"><span>Platform workspace <span className="muted">/ {pages[page].name}</span></span>
      <div className="inline">{session.expiresAt && <small>Session until {new Date(session.expiresAt).toLocaleTimeString()}</small>}
        <Button size="small" onClick={signOut}>Sign out</Button></div></header>
      <main id="main" className="main-content"><div className="page-heading"><div><span className="eyebrow">GATEWAY INTELLIGENCE</span>
        <h1>{pages[page].name}</h1><p>{pages[page].subtitle}</p></div>
        {portal.value && <div className="scope-pill"><span className="connection-dot" /><div>{portal.value.displayName}<small>{portal.value.scope.environment} / {portal.value.scope.hubId}</small></div></div>}
      </div>
      {error && <Notice error>{error}</Notice>}
      <LoadState {...portal} retry={portal.refresh} />
      {portal.value && <PageContent page={page} api={api} portal={portal.value} subjectId={session.user?.subjectId ?? ""}
        investigationPrompt={investigationPrompt} onPromptConsumed={() => setInvestigationPrompt(undefined)}
        onInvestigate={(prompt) => { setInvestigationPrompt(prompt); navigate("assistant"); }} />}
      <footer className="page-footer">Citadel | Model costs are estimates. Missing observations are never reported as healthy or free.</footer>
      </main>
    </div>
  </div>;
}

function PageContent({ page, api, portal, subjectId, investigationPrompt, onPromptConsumed, onInvestigate }: {
  page: Page; api: PortalApi; portal: Portal; subjectId: string; investigationPrompt?: string;
  onPromptConsumed: () => void; onInvestigate: (prompt: string) => void;
}) {
  const action = page === "finops" ? "finops.read" : page === "operations" ? "operations.read" : "assistant.use";
  if (!portal.allowedActions.includes(action)) return <Notice>This page requires additional application roles or an explicit hub data grant. Contact your platform administrator.</Notice>;
  if (page === "finops") return <FinOpsPage api={api} portal={portal} />;
  if (page === "operations") return <OperationsPage api={api} portal={portal} onInvestigate={onInvestigate} />;
  return <Assistant api={api} portal={portal} subjectId={subjectId} initialPrompt={investigationPrompt} onPromptConsumed={onPromptConsumed} />;
}

function currentPage(): Page {
  const path = window.location.pathname.slice(1);
  return path === "finops" || path === "operations" ? path : "assistant";
}
