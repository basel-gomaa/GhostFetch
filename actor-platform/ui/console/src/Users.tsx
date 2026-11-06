import { Fragment, useEffect, useState } from "react";
import {
  AdminAction, AdminChange, AdminUser, AdminUserDetail, DisableResult, Invite, User, api,
  inviteLink,
} from "./api";
import { Banner, EmptyState, ErrorBanner, GhostLoader, PageHeader, fmtTime, useLoad } from "./ui";

/** User administration: promote, demote, disable, enable.
 *
 *  EVERY RULE HERE IS ALSO THE SERVER'S. Hiding your own Demote/Disable and
 *  hiding this screen from non-admins are courtesies; api/admin.py refuses all
 *  of it regardless (403 for a non-admin, 409 for yourself or the last admin),
 *  and those refusals are shown verbatim rather than second-guessed here.
 *
 *  NOTHING IS OPTIMISTIC. A row changes only once the server has said so, and
 *  the outcome banner is built from the server's own answer -- what disabling
 *  actually cancelled and stopped, not what it was expected to. */
export function Users({ me }: { me: User }) {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  // Server-side search, debounced so typing a name is one request, not six.
  useEffect(() => {
    const t = window.setTimeout(() => setSearch(query.trim()), 250);
    return () => window.clearTimeout(t);
  }, [query]);

  const users = useLoad<AdminUser[]>(() => api.adminUsers(search), [search]);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<AdminUserDetail | null>(null);

  const act = async (u: AdminUser, action: AdminAction) => {
    setBusy(u.id); setError(null); setOutcome(null); setConfirming(null);
    try {
      const result = await api.adminAction<AdminChange & Partial<DisableResult>>(u.id, action);
      setOutcome(describe(u.username, action, result));
      users.reload();
      if (open === u.id) api.adminUser(u.id).then(setDetail).catch(() => {});
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const toggle = (u: AdminUser) => {
    if (open === u.id) { setOpen(null); return; }
    setOpen(u.id); setDetail(null);
    api.adminUser(u.id).then(setDetail).catch(setError);
  };

  return (
    <div className="stack">
      <PageHeader title="Users" sub={<>
          Everyone with an account. Promote someone to administrator, or disable
          an account — disabling is reversible and keeps their history.
        </>} />

      <input style={{ maxWidth: 320 }} placeholder="Search by email or username"
             value={query} onChange={(e) => setQuery(e.target.value)} />

      {outcome ? <Banner kind="ok">{outcome}</Banner> : null}
      <ErrorBanner error={error ?? users.error} />

      {users.data?.length ? (
        <table>
          <thead>
            <tr><th>User</th><th>Email</th><th>Role</th><th>Status</th><th>Joined</th><th /></tr>
          </thead>
          <tbody>
            {users.data.map((u) => {
              const self = u.id === me.id;
              return (
                <Fragment key={u.id}>
                  <tr style={{ cursor: "pointer" }} onClick={() => toggle(u)}>
                    <td>
                      <strong>{u.username}</strong>
                      {self ? <span className="muted"> (you)</span> : null}
                      {/* Their chosen name, beside -- never instead of -- the username. */}
                      {u.display_name ? <div className="muted">{u.display_name}</div> : null}
                    </td>
                    <td>{u.email}</td>
                    <td>{u.is_admin ? <span className="badge admin">admin</span>
                                    : <span className="muted">user</span>}</td>
                    <td>{u.disabled ? <span className="badge disabled">disabled</span>
                                    : <span className="badge connected">active</span>}
                        {u.locked ? <> <span className="badge disabled"
                          title="Too many failed sign-ins. Clears by itself, or unlock it now.">locked</span></> : null}</td>
                    <td>{fmtTime(u.created_at)}</td>
                    <td onClick={(e) => e.stopPropagation()} style={{ whiteSpace: "nowrap" }}>
                      {self ? (
                        <span className="muted" title="Ask another administrator to change your own account">
                          —
                        </span>
                      ) : (
                        <div className="row" style={{ gap: 14 }}>
                          {u.is_admin
                            ? <button className="link" disabled={busy === u.id}
                                      onClick={() => act(u, "demote")}>Demote</button>
                            : <button className="link" disabled={busy === u.id}
                                      onClick={() => act(u, "promote")}>Promote</button>}
                          {u.locked
                            ? <button className="link" disabled={busy === u.id}
                                      onClick={() => act(u, "unlock")}>Unlock</button>
                            : null}
                          {u.disabled
                            ? <button className="link" disabled={busy === u.id}
                                      onClick={() => act(u, "enable")}>Enable</button>
                            : <button className="danger link" disabled={busy === u.id}
                                      onClick={() => { setOutcome(null); setError(null);
                                                       setConfirming(u.id); }}>Disable</button>}
                        </div>
                      )}
                    </td>
                  </tr>

                  {confirming === u.id ? (
                    <tr>
                      <td colSpan={6}>
                        <Banner kind="warn">
                          <div style={{ marginBottom: 8 }}>
                            Disable <strong>{u.username}</strong>? This signs them out
                            everywhere and cancels their queued and running runs. Their API keys stop working
                            until you enable them again. Their run history and connected
                            accounts are kept.
                          </div>
                          <div className="row">
                            <button className="danger" disabled={busy === u.id}
                                    onClick={() => act(u, "disable")}>Disable {u.username}</button>
                            <button onClick={() => setConfirming(null)}>Cancel</button>
                          </div>
                        </Banner>
                      </td>
                    </tr>
                  ) : null}

                  {open === u.id ? (
                    <tr>
                      <td colSpan={6} className="muted">
                        {detail && detail.id === u.id ? (
                          <>
                            {detail.live_sessions} live session(s) · {detail.api_keys} active
                            API key(s) · {detail.runs} run(s)
                            {detail.disabled_at ? <> · disabled {fmtTime(detail.disabled_at)}</> : null}
                          </>
                        ) : <><span className="spin" /> Loading…</>}
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      ) : users.loading ? <GhostLoader label="Loading users…" />
        : <EmptyState compact title={search ? "No user matches that search." : "No users yet."} />}

      <Invites />
    </div>
  );
}

const MODE_TEXT = {
  invite: "Sign-up is by invitation: a new account needs one of these links.",
  open: "Sign-up is OPEN (ACTOR_PLATFORM_SIGNUP=open): anyone can create an account, "
      + "so invites are not needed.",
  closed: "Sign-up is CLOSED (ACTOR_PLATFORM_SIGNUP=closed): nobody can create an "
        + "account, not even with an invite.",
} as const;

/** Single-use invite links (api/invites.py). The link is shown ONCE: the server
 *  keeps only a hash of it, so a lost link is revoked and replaced, not recovered. */
function Invites() {
  const list = useLoad(() => api.adminInvites(), []);
  const [note, setNote] = useState("");
  const [fresh, setFresh] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const create = async () => {
    setBusy("new"); setError(null); setCopied(false);
    try {
      const made = await api.createInvite(note.trim());
      setFresh(inviteLink(made.token));
      setNote("");
      list.reload();
    } catch (e) { setError(e); } finally { setBusy(null); }
  };

  const revoke = async (inv: Invite) => {
    setBusy(inv.id); setError(null);
    try { await api.revokeInvite(inv.id); list.reload(); }
    catch (e) { setError(e); } finally { setBusy(null); }
  };

  const copy = async () => {
    if (!fresh) return;
    try { await navigator.clipboard.writeText(fresh); setCopied(true); } catch { /* select it by hand */ }
  };

  const mode = list.data?.signup;
  return (
    <div className="stack" style={{ marginTop: 24 }}>
      <div>
        <h2 style={{ margin: 0 }}>Invites</h2>
        <p className="sub" style={{ margin: 0 }}>
          {mode ? MODE_TEXT[mode] : "Single-use sign-up links."} Each link works once
          and expires after a week.
        </p>
      </div>

      {fresh ? (
        <Banner kind="ok">
          <div>Send this link to the person you are inviting. Copy it now — it will not be shown again.</div>
          <code style={{ userSelect: "all", wordBreak: "break-all" }}>{fresh}</code>
          <div className="row" style={{ marginTop: 8 }}>
            <button onClick={copy}>{copied ? "Copied" : "Copy link"}</button>
            <button className="link" onClick={() => setFresh(null)}>Done</button>
          </div>
        </Banner>
      ) : null}
      <ErrorBanner error={error ?? list.error} />

      <div className="row">
        <input className="grow" style={{ maxWidth: 280 }} maxLength={200}
               placeholder="Who is it for? (only admins see this)"
               value={note} onChange={(e) => setNote(e.target.value)} />
        <button className="primary" onClick={create} disabled={busy === "new"}>
          Create invite link
        </button>
      </div>

      {list.data?.invites.length ? (
        <table>
          <thead>
            <tr><th>For</th><th>Status</th><th>Created</th><th>Expires</th><th>Used by</th><th /></tr>
          </thead>
          <tbody>
            {list.data.invites.map((inv) => (
              <tr key={inv.id}>
                <td>{inv.note ?? <span className="muted">—</span>}
                    {inv.created_by ? <span className="muted"> · by {inv.created_by}</span> : null}</td>
                <td><span className={`badge ${inv.status === "pending" ? "connected"
                                       : inv.status === "used" ? "admin" : "disabled"}`}>
                  {inv.status}</span></td>
                <td>{fmtTime(inv.created_at)}</td>
                <td>{inv.status === "pending" ? fmtTime(inv.expires_at) : <span className="muted">—</span>}</td>
                <td>{inv.used_by ?? (inv.used_at ? <span className="muted">deleted user</span>
                                                  : <span className="muted">—</span>)}</td>
                <td>
                  {inv.status === "pending"
                    ? <button className="danger link" disabled={busy === inv.id}
                              onClick={() => revoke(inv)}>Revoke</button>
                    : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : list.loading ? <GhostLoader label="Loading invites…" />
        : <EmptyState compact title="No invites yet">Create an invite link above to add someone.</EmptyState>}
    </div>
  );
}

/** The banner text, from what the SERVER says happened. */
function describe(name: string, action: AdminAction,
                  r: AdminChange & Partial<DisableResult>): string {
  const past = { promote: "Promoted", demote: "Demoted", disable: "Disabled",
                 enable: "Enabled", unlock: "Unlocked" }[action];
  const state = { promote: "an administrator", demote: "not an administrator",
                  disable: "disabled", enable: "enabled", unlock: "unlocked" }[action];
  if (action !== "disable") {
    return r.already ? `${name} was already ${state}.` : `${past} ${name}.`;
  }
  const parts: string[] = [];
  const cancelled = r.runs_cancelled ?? 0;
  const browsers = (r.login_browsers_stopped ?? 0) + (r.login_browsers_unconfirmed ?? 0);
  if (cancelled) parts.push(`cancelled ${cancelled} run(s)`);
  if (browsers) parts.push(`stopped ${browsers} login browser(s)`);
  const head = r.already ? `${name} was already disabled` : `Disabled ${name}`;
  return parts.length ? `${head}: ${parts.join(", ")}.` : `${head}.`;
}
