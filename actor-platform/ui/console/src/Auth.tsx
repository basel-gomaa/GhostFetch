import { useEffect, useState } from "react";
import { api, SignupOptions, User } from "./api";
import { GhostMark, Wordmark } from "./brand/Logo";
import { IconCheck } from "./brand/icons";
import { ErrorBanner, ThemeSwitch } from "./ui";

/** The invite token from an invite link, `/#invite=<token>`, read once.
 *
 *  A FRAGMENT, never a query string: a browser does not send the part after #
 *  to the server, so the token cannot reach the API's access log (api.log),
 *  where a `?invite=` link was written out in full and anyone who could read
 *  the log could have used it. It is taken at MODULE LOAD, before App's hash
 *  router first reads location.hash, and removed from the address bar at once.
 *  The server decides whether it is valid, never this page. */
function takeInviteFromUrl(): string {
  const m = /^#invite=([^&]+)/.exec(window.location.hash);
  if (!m) return "";
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  try { return decodeURIComponent(m[1]); } catch { return m[1]; }
}

const INVITE_FROM_LINK = takeInviteFromUrl();

export function Auth({ onSignedIn }: { onSignedIn: (u: User) => void }) {
  const [invite, setInvite] = useState(INVITE_FROM_LINK);
  const [mode, setMode] = useState<"login" | "signup">(invite ? "signup" : "login");
  const [email, setEmail] = useState("");
  const [identifier, setIdentifier] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  // What the form should ask for. Only presentation: /auth/signup enforces it.
  const [options, setOptions] = useState<SignupOptions | null>(null);

  useEffect(() => { api.signupOptions().then(setOptions).catch(() => setOptions(null)); }, []);
  // An invite link pasted into a tab that already has the console open changes
  // only the fragment, so there is no fresh load to read it on.
  useEffect(() => {
    const on = () => {
      const token = takeInviteFromUrl();
      if (token) { setInvite(token); setMode("signup"); setError(null); }
    };
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);

  const closed = options?.signup === "closed";
  const askInvite = !!invite || !!options?.invite_required;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      onSignedIn(mode === "login"
        ? await api.login(identifier, password)
        : await api.signup(email, username, password, invite.trim() || undefined));
    } catch (err) { setError(err); }
    finally { setBusy(false); }
  };

  return (
    <div className="auth">
      <aside className="auth-brand">
        <GhostMark className="big-mark" size={520} animated />
        <Wordmark size={22} />
        <div>
          <h2>Public data, fetched quietly.</h2>
          <p>
            Run isolated scrapers against public pages and profiles, then get
            structured data and an AI-written report back.
          </p>
          <ul>
            <li><IconCheck /> Every run in its own container, with live logs</li>
            <li><IconCheck /> Results as CSV, JSON and a shareable report</li>
            <li><IconCheck /> Sessions stored encrypted, never your password</li>
          </ul>
        </div>
        <div className="foot">GhostFetch</div>
      </aside>

      <div className="auth-form">
      <div className="inner stack">
      <div className="mobile-brand"><Wordmark size={20} /></div>
      <div className="row between" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
        <div>
          <h1>{mode === "login" ? "Sign in to GhostFetch" : "Create your GhostFetch account"}</h1>
          <p className="sub" style={{ margin: 0 }}>
            {mode === "login" ? "Welcome back. Sign in to your account." : "Create an account. Reports go to the address you use here."}
          </p>
        </div>
        <ThemeSwitch compact />
      </div>
      <ErrorBanner error={error} />
      <form className="card stack" onSubmit={submit}>
        {mode === "signup" ? (
          <>
            {askInvite ? (
              <div>
                <label htmlFor="invite">Invite</label>
                <input id="invite" required={!!options?.invite_required} autoComplete="off"
                       spellCheck={false} value={invite}
                       onChange={(e) => setInvite(e.target.value)} />
                <div className="hint">
                  Sign-up here is by invitation. Open the invite link an administrator
                  sent you, or paste its code.
                </div>
              </div>
            ) : null}
            <div>
              <label htmlFor="email">Email</label>
              <input id="email" type="email" autoComplete="email" required
                     value={email} onChange={(e) => setEmail(e.target.value)} />
              <div className="hint">Where your reports are sent.</div>
            </div>
            <div>
              <label htmlFor="username">Username</label>
              <input id="username" required autoComplete="username"
                     value={username} onChange={(e) => setUsername(e.target.value)} />
            </div>
          </>
        ) : (
          <div>
            <label htmlFor="identifier">Email or username</label>
            <input id="identifier" required autoComplete="username"
                   value={identifier} onChange={(e) => setIdentifier(e.target.value)} />
          </div>
        )}
        <div>
          <label htmlFor="password">Password</label>
          <input id="password" type="password" required minLength={10}
                 autoComplete={mode === "login" ? "current-password" : "new-password"}
                 value={password} onChange={(e) => setPassword(e.target.value)} />
          {mode === "signup" ? <div className="hint">At least 10 characters.</div> : null}
        </div>
        <button className="primary" type="submit" disabled={busy}>
          {busy ? <span className="spin" /> : null}
          {busy ? (mode === "login" ? "Signing in…" : "Creating account…") : mode === "login" ? "Sign in" : "Create account"}
        </button>
      </form>
      {closed && mode === "login" ? (
        <p className="muted">Sign-up is closed on this platform.</p>
      ) : (
        <button className="link" onClick={() => { setMode(mode === "login" ? "signup" : "login"); setError(null); }}>
          {mode === "login" ? "Create an account instead" : "I already have an account"}
        </button>
      )}
      </div>
      </div>
    </div>
  );
}
