import { useEffect, useState } from "react";
import { api, RunSummary, TERMINAL, User } from "./api";
import { Accounts } from "./Accounts";
import { AccountPage } from "./Account";
import { Auth } from "./Auth";
import { Dashboard } from "./Dashboard";
import { RunForm } from "./RunForm";
import { RunView } from "./RunView";
import { Users } from "./Users";
import { Wordmark } from "./brand/Logo";
import { ActorName } from "./brand/PlatformLogo";
import {
  IconClose, IconDashboard, IconEdit, IconLink, IconLogout, IconMenu, IconPlay, IconRuns, IconUser,
  IconUsers,
} from "./brand/icons";
import {
  Avatar, Badge, EmptyState, ErrorBanner, GhostLoader, PageHeader, ThemeQuickToggle, ThemeSwitch, displayName, fmtDuration, fmtTime,
  useLoad, usePolling,
} from "./ui";

/** Hash routing. No router dependency for a handful of screens, and it means
 *  the built console is a static bundle the API can serve from one path with no
 *  rewrite rules for deep links. */
function useRoute() {
  // Only "#/<screen>" is a route. Anything else in the fragment (an invite
  // link's "#invite=...", read by Auth.tsx) is not, and means the default.
  const current = () => (location.hash.startsWith("#/") && location.hash.slice(2)) || "dashboard";
  const [route, setRoute] = useState(current);
  useEffect(() => {
    const on = () => setRoute(current());
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return [route, (r: string) => { location.hash = `#/${r}`; }] as const;
}

function Runs({ onOpen }: { onOpen: (id: string) => void }) {
  const runs = useLoad<RunSummary[]>(() => api.runs(), []);
  const anyActive = !!runs.data?.some((r) => !TERMINAL.has(r.status));
  usePolling(() => api.runs().then(runs.setData).catch(() => {}), 5000, anyActive);
  const newRun = () => { location.hash = "#/new"; };

  return (
    <div className="stack">
      <PageHeader
        title="Runs"
        sub="Every run you have started, newest first. Open one for its logs, data and report."
        actions={<button className="primary" onClick={newRun}>New run</button>}
      />
      <ErrorBanner error={runs.error} />
      {runs.data?.length ? (
        <table>
          <thead><tr><th>Run</th><th>Scraper</th><th>Status</th><th>Started</th><th>Took</th></tr></thead>
          <tbody>
            {runs.data.map((r) => (
              <tr key={r.run_id} style={{ cursor: "pointer" }} onClick={() => onOpen(r.run_id)}>
                <td><code>{r.run_id.slice(0, 21)}</code></td>
                <td><ActorName name={r.actor} size={26} strong={false} /></td>
                <td><Badge value={r.status} /></td>
                <td>{fmtTime(r.started_at ?? r.created_at)}</td>
                <td>{fmtDuration(r.duration_seconds)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : runs.loading ? <GhostLoader label="Loading runs…" />
        : (
          <EmptyState title="No runs yet"
                      action={<button className="primary" onClick={newRun}>Start your first run</button>}>
            Pick a scraper, point it at a page or profile, and GhostFetch collects and
            analyses it for you.
          </EmptyState>
        )}
    </div>
  );
}

export function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [route, go] = useRoute();
  const [openRun, setOpenRun] = useState<string | null>(null);
  const [menu, setMenu] = useState(false);

  useEffect(() => { api.me().then(setUser).catch(() => setUser(null)); }, []);
  // The drawer is a mobile affordance; any navigation closes it.
  useEffect(() => { setMenu(false); }, [route, openRun]);
  useEffect(() => {
    if (!menu) return;
    const on = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(false); };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [menu]);

  if (user === undefined) return <GhostLoader fullscreen label="Waking up GhostFetch…" />;
  if (user === null) return <Auth onSignedIn={setUser} />;

  const open = (id: string) => { setOpenRun(id); go("run"); };
  const nav = (id: string, label: string, icon: React.ReactNode) => {
    // A run's detail page belongs to Runs, so Runs stays lit there.
    const on = route === id || (id === "runs" && route === "run");
    return (
      <a className={`nav ${on ? "on" : ""}`} href={`#/${id}`} aria-current={on ? "page" : undefined}>
        {icon}{label}
      </a>
    );
  };

  return (
    <div className="shell">
      <header className="topbar">
        <a href="#/dashboard" className="brand" style={{ padding: 0 }}><Wordmark size={17} /></a>
        <div className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
          <ThemeQuickToggle />
          <button className="icon-btn" onClick={() => setMenu(true)} aria-label="Open navigation"
                  aria-expanded={menu}>
            <IconMenu />
          </button>
        </div>
      </header>
      {menu ? <div className="scrim" onClick={() => setMenu(false)} /> : null}

      <nav className={`side${menu ? " open" : ""}`} aria-label="Main">
        <div className="row between" style={{ flexWrap: "nowrap" }}>
          <a href="#/dashboard" className="brand"><Wordmark size={19} /></a>
          <button className="icon-btn drawer-close" style={{ marginBottom: 20 }} onClick={() => setMenu(false)}
                  aria-label="Close navigation"><IconClose /></button>
        </div>
        <div className="nav-label">Workspace</div>
        {nav("dashboard", "Dashboard", <IconDashboard />)}
        {nav("new", "New run", <IconPlay />)}
        {nav("runs", "Runs", <IconRuns />)}
        <div className="nav-label">Settings</div>
        {nav("accounts", "Connected accounts", <IconLink />)}
        {nav("account", "Profile & API keys", <IconUser />)}
        {/* Only offered to administrators. Presentation only: the API refuses a
            non-admin on every /admin route whatever the console shows. */}
        {user.is_admin ? nav("users", "Users", <IconUsers />) : null}
        <div className="side-foot">
          {/* The role is SHOWN, not only implied by the Users entry: otherwise a
              stale console and a non-admin account look exactly alike. */}
          {/* The chip IS the way into the profile: photo and name are edited
              on the Account screen. */}
          <a className="user-chip" href="#/account" title="Edit your name and photo">
            <span className="avatar-wrap">
              <Avatar user={user} size={36} />
              <span className="avatar-edit" aria-hidden><IconEdit size={11} /></span>
            </span>
            <span className="who">
              <strong>{displayName(user)}</strong>
              <span className="handle">@{user.username}</span>
              {user.is_admin ? <span className="badge admin">admin</span> : null}
            </span>
          </a>
          <div>
            <div className="nav-label" style={{ padding: "0 0 6px" }}>Theme</div>
            <ThemeSwitch />
          </div>
          <div className="side-actions">
            <button className="link" onClick={() => api.logout().then(() => setUser(null))}>
              <IconLogout size={16} /> Sign out
            </button>
          </div>
        </div>
      </nav>

      <main className="main">
        <div className="page" key={route}>
          {route === "dashboard" ? <Dashboard user={user} onOpen={open} /> : null}
          {route === "runs" ? <Runs onOpen={open} /> : null}
          {route === "new" ? <RunForm onStarted={open} /> : null}
          {route === "accounts" ? <Accounts /> : null}
          {route === "account" ? <AccountPage user={user} onUser={setUser} /> : null}
          {route === "users" && user.is_admin ? <Users me={user} /> : null}
          {route === "users" && !user.is_admin
            ? <EmptyState title="Administrators only">This screen manages other people's accounts.</EmptyState>
            : null}
          {route === "run" && openRun ? <RunView runId={openRun} onBack={() => go("runs")} /> : null}
          {route === "run" && !openRun
            ? <EmptyState title="No run selected"
                          action={<button onClick={() => go("runs")}>Go to Runs</button>}>
                Pick a run from the list to see its logs, data and report.
              </EmptyState>
            : null}
          {!KNOWN.has(route)
            ? <EmptyState title="Nothing here"
                          action={<button onClick={() => go("dashboard")}>Back to the dashboard</button>}>
                This page does not exist.
              </EmptyState>
            : null}
        </div>
      </main>
    </div>
  );
}

const KNOWN = new Set(["dashboard", "runs", "new", "accounts", "account", "users", "run"]);
