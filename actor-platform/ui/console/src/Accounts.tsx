import { useState } from "react";
import { api, PlatformOption, SocialAccount } from "./api";
import { PlatformName } from "./brand/PlatformLogo";
import { useCatalog } from "./catalog";
import { Badge, Banner, EmptyState, ErrorBanner, GhostLoader, PageHeader, fmtTime, useLoad } from "./ui";

/**
 * Connecting an account (DEMO BUILD).
 *
 * In the real build, Connect opened a browser on the server and streamed it
 * into this page so the user could sign in. The public demo has no login
 * browser: Connect starts a demo login, which the API reports as signed in at
 * once, and saves it -- a placeholder session stored encrypted, exactly where
 * a real one would go. Nothing is signed in to any real site.
 */
export function Accounts() {
  const accounts = useLoad<SocialAccount[]>(() => api.accounts(), []);
  const platforms = useLoad<PlatformOption[]>(() => api.platforms(), []);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  // Which platform's Connect was pressed, so only that card says "Connecting…".
  const [starting, setStarting] = useState<string | null>(null);
  const [connected, setConnected] = useState<string | null>(null);
  const options = platforms.data ?? [];
  const catalog = useCatalog();

  const start = async (platform: string) => {
    setBusy(true); setStarting(platform); setError(null); setConnected(null);
    try {
      const session = await api.startLogin(platform);
      await api.capture(session.id);
      setConnected(platform);
      accounts.reload();
    } catch (e) { setError(e); }
    finally { setBusy(false); setStarting(null); }
  };

  return (
    <div className="stack">
      <PageHeader title="Connected accounts" sub={<>
          A scrape runs as you. Connect the account you want it to use.
        </>} />

      <Banner kind="warn">
        Demo build: Connect does not sign in to anything. It stores a placeholder
        session so runs that need an account can start, and the scrapers return
        synthetic data.
      </Banner>

      {connected ? (
        <Banner kind="ok">
          {catalog.platform(connected)?.label ?? connected} demo account connected.
        </Banner>
      ) : null}

      <ErrorBanner error={error} />
      <ErrorBanner error={accounts.error} />

      {accounts.data?.length ? (
        <div className="cards">
          {accounts.data.map((a) => (
            <div key={a.platform} className="card row between">
              <div>
                <div className="row" style={{ gap: 10 }}>
                  <PlatformName name={a.platform} size={36} /> <Badge value={a.status} />
                </div>
                <div className="muted" style={{ marginTop: 6 }}>
                  {a.external_id ? <>account {a.external_id} · </> : null}
                  connected {fmtTime(a.connected_at)}
                  {a.last_verified_at ? <> · last checked {fmtTime(a.last_verified_at)}</> : null}
                </div>
                {a.needs_reconnect ? (
                  <div style={{ color: "var(--bad)" }}>
                    Needs reconnecting{a.expired_reason ? ` (${a.expired_reason})` : ""}.
                  </div>
                ) : null}
              </div>
              <div className="row">
                <button onClick={() => start(a.platform)} disabled={busy}>
                  {a.needs_reconnect ? "Reconnect" : "Reconnect"}
                </button>
                <button className="danger" disabled={busy}
                        onClick={() => api.disconnect(a.platform).then(accounts.reload).catch(setError)}>
                  Disconnect
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : accounts.loading ? <GhostLoader label="Loading accounts…" /> : (
        <EmptyState compact title="Nothing connected yet">
          Connect an account below so your scrapers can run as you.
        </EmptyState>
      )}

      <div className="card stack">
      <h3>Connect a new account</h3>
      {/* One card per platform, each with its own Connect. A platform whose
          public data needs no sign-in says why instead of offering a button
          that would capture nothing. */}
      <div className="picker">
        {options.map((p) => (
          <div key={p.name} className="card platform-card">
            <PlatformName name={p.name} size={34} />
            {p.connectable ? (
              <button className={accounts.data?.some((a) => a.platform === p.name) ? "" : "primary"}
                      disabled={busy} onClick={() => start(p.name)}>
                {busy && starting === p.name ? <><span className="spin" /> Connecting…</>
                  : accounts.data?.some((a) => a.platform === p.name) ? "Reconnect" : "Connect"}
              </button>
            ) : (
              <p className="note">{p.note ?? "Needs no sign-in."}</p>
            )}
          </div>
        ))}
      </div>
      {platforms.data && !platforms.data.length ? (
        <span className="muted">No platforms are configured.</span>
      ) : null}
      </div>
    </div>
  );
}
