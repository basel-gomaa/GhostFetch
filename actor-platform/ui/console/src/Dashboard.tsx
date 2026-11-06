import { Actor, api, RunSummary, SocialAccount, TERMINAL, User } from "./api";
import { GhostMark } from "./brand/Logo";
import { ActorName, BrandName, PlatformName } from "./brand/PlatformLogo";
import { IconArrowRight, IconLink, IconPlay, IconRuns, IconUsers } from "./brand/icons";
import {
  Badge, EmptyState, ErrorBanner, GhostLoader, StatCard, displayName, fmtDuration, fmtTime, useLoad, usePolling,
} from "./ui";

/**
 * The landing screen. EVERY NUMBER HERE IS COUNTED FROM WHAT THE API RETURNED,
 * and says so: GET /runs is a page (the console asks for the newest 50), so the
 * stats describe "your last N runs", never a lifetime total the API was not
 * asked for. Each source loads and fails on its own, so a broken account list
 * does not blank the run history.
 */
export function Dashboard({ user, onOpen }: { user: User; onOpen: (id: string) => void }) {
  const runs = useLoad<RunSummary[]>(() => api.runs(), []);
  const actors = useLoad<Actor[]>(() => api.actors(), []);
  const accounts = useLoad<SocialAccount[]>(() => api.accounts(), []);

  const list = runs.data ?? [];
  const active = list.filter((r) => !TERMINAL.has(r.status));
  // Same rule as the Runs screen: poll only while something can still change.
  usePolling(() => api.runs().then(runs.setData).catch(() => {}), 5000, active.length > 0);

  const count = (...s: string[]) => list.filter((r) => s.includes(r.status)).length;
  const succeeded = count("SUCCEEDED");
  const failed = count("FAILED", "PLATFORM_ERROR");
  const cancelled = count("CANCELLED");
  const finished = succeeded + failed + cancelled;
  const scrapers = (actors.data ?? []).filter((a) => !a.hidden);
  const connected = (accounts.data ?? []).filter((a) => a.status === "connected");
  const reconnect = (accounts.data ?? []).filter((a) => a.needs_reconnect);
  const go = (r: string) => { location.hash = `#/${r}`; };

  return (
    <div className="stack" style={{ gap: 20 }}>
      <section className="hero">
        <GhostMark className="streams" size={320} animated />
        <h1>Welcome back, {displayName(user)}</h1>
        <p className="sub">
          GhostFetch collects public social media data with real, isolated scrapers,
          then turns it into structured datasets and AI-written reports. Start a run,
          watch it live, and download the results when it finishes.
        </p>
        <div className="quick">
          <a href="#/new" className="primary-action">
            <span className="ico"><IconPlay /></span>
            <span>New run<small>Pick a scraper and a target</small></span>
          </a>
          <a href="#/runs">
            <span className="ico"><IconRuns /></span>
            <span>All runs<small>History, logs and results</small></span>
          </a>
          <a href="#/accounts">
            <span className="ico"><IconLink /></span>
            <span>Connected accounts<small>Sessions your scrapers use</small></span>
          </a>
          {user.is_admin ? (
            <a href="#/users">
              <span className="ico"><IconUsers /></span>
              <span>Users<small>Members and invites</small></span>
            </a>
          ) : null}
        </div>
      </section>

      <ErrorBanner error={runs.error} />

      {runs.loading && !runs.data ? <GhostLoader label="Fetching your runs…" /> : runs.data ? (
        <>
          <div>
            <div className="stat-grid">
              <StatCard tone="run" label="Active" value={active.length}
                        hint={active.length ? "running or queued now" : "nothing running"} />
              <StatCard tone="ok" label="Succeeded" value={succeeded}
                        hint={finished ? `${Math.round((succeeded / finished) * 100)}% of finished runs` : "no finished runs yet"} />
              <StatCard tone="bad" label="Failed" value={failed}
                        hint={failed ? "open one to see why" : "none"} />
              <StatCard tone="muted" label="Cancelled" value={cancelled} />
            </div>
            {list.length ? (
              <p className="hint" style={{ marginTop: 8 }}>
                Counted over your {list.length === 1 ? "latest run" : `latest ${list.length} runs`}.
              </p>
            ) : null}
          </div>

          <div className="dash-grid">
            <div className="stack">
              <div className="card">
                <div className="card-head">
                  <h3>Running &amp; pending</h3>
                  {active.length ? <span className="badge RUNNING"><span className="dot" />{active.length} active</span> : null}
                </div>
                {active.length ? (
                  <div className="list">
                    {active.map((r) => (
                      <button key={r.run_id} className="list-item" onClick={() => onOpen(r.run_id)}>
                        <span style={{ minWidth: 0 }}>
                          <span className="title"><ActorName name={r.actor} size={24} /></span>
                          <span className="hint" style={{ display: "block", margin: 0 }}>
                            <code>{r.run_id.slice(0, 21)}</code> · started {fmtTime(r.started_at ?? r.created_at)}
                          </span>
                        </span>
                        <Badge value={r.status} />
                      </button>
                    ))}
                  </div>
                ) : (
                  <EmptyState compact title="All quiet">
                    No run is in progress. New runs show up here while they work.
                  </EmptyState>
                )}
              </div>

              <div className="card">
                <div className="card-head">
                  <h3>Recent runs</h3>
                  {list.length ? <a href="#/runs" className="row" style={{ gap: 4 }}>View all <IconArrowRight size={14} /></a> : null}
                </div>
                {list.length ? (
                  <table className="table-bare">
                    <thead><tr><th>Scraper</th><th>Status</th><th>Started</th><th>Took</th></tr></thead>
                    <tbody>
                      {list.slice(0, 8).map((r) => (
                        <tr key={r.run_id} style={{ cursor: "pointer" }} onClick={() => onOpen(r.run_id)}>
                          <td><ActorName name={r.actor} size={24} /></td>
                          <td><Badge value={r.status} />{r.partial ? <> <Badge value="PARTIAL" /></> : null}</td>
                          <td>{fmtTime(r.started_at ?? r.created_at)}</td>
                          <td>{fmtDuration(r.duration_seconds)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <EmptyState compact title="No runs yet"
                              action={<button className="primary" onClick={() => go("new")}>Start your first run</button>}>
                    Your run history appears here.
                  </EmptyState>
                )}
              </div>
            </div>

            <div className="stack">
              <div className="card">
                <div className="card-head">
                  <h3>Scrapers</h3>
                  {scrapers.length ? <span className="muted">{scrapers.length} available</span> : null}
                </div>
                <ErrorBanner error={actors.error} />
                {actors.loading ? <GhostLoader /> : scrapers.length ? (
                  <div className="list">
                    {scrapers.map((a) => (
                      <div key={a.name} className="list-item">
                        <span className="title"><BrandName icon={a.icon} label={a.label} name={a.name} size={28} /></span>
                        <a href="#/new">Run</a>
                      </div>
                    ))}
                  </div>
                ) : !actors.error ? <p className="muted" style={{ margin: 0 }}>No scrapers are installed.</p> : null}
              </div>

              <div className="card">
                <div className="card-head">
                  <h3>Connected accounts</h3>
                  <a href="#/accounts">Manage</a>
                </div>
                <ErrorBanner error={accounts.error} />
                {accounts.loading ? <GhostLoader /> : accounts.data?.length ? (
                  <div className="list">
                    {accounts.data.map((a) => (
                      <div key={a.platform} className="list-item">
                        <span className="title"><PlatformName name={a.platform} size={24} /></span>
                        <Badge value={a.status} />
                      </div>
                    ))}
                  </div>
                ) : !accounts.error ? (
                  <p className="muted" style={{ margin: 0 }}>
                    None yet. Most scrapers run as one of your accounts.{" "}
                    <a href="#/accounts">Connect one</a>
                  </p>
                ) : null}
                {reconnect.length ? (
                  <p className="hint" style={{ color: "var(--bad)" }}>
                    {reconnect.length === 1 ? "1 account needs" : `${reconnect.length} accounts need`} reconnecting.
                  </p>
                ) : accounts.data?.length ? (
                  <p className="hint">{connected.length} of {accounts.data.length} connected.</p>
                ) : null}
              </div>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
