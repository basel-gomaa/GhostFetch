import { useEffect, useRef, useState } from "react";
import { api, ApiError, User } from "./api";
import { GhostMark } from "./brand/Logo";
import { IconMonitor, IconMoon, IconSun } from "./brand/icons";

/** A run or account status. The class IS the status name -- styles.css keys
 *  its colours on it -- and the dot is decoration in the same colour. */
export function Badge({ value }: { value: string }) {
  return <span className={`badge ${value}`}><span className="dot" aria-hidden />{value}</span>;
}

/** What to call a person: the name they chose, else their username. */
export function displayName(user: Pick<User, "display_name" | "username">) {
  return user.display_name || user.username;
}

function initials(name: string) {
  const words = name.trim().split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[words.length - 1][0] : (words[0] ?? "?").slice(0, 2);
  return letters.toUpperCase();
}

/** The profile photo, or the person's initials when there is none (or it
 *  fails to load). Keyed on the photo's version so a new upload replaces it. */
export function Avatar({ user, size = 32 }: { user: User; size?: number }) {
  const [broken, setBroken] = useState<string | null>(null);
  const version = user.avatar_version ?? null;
  const box = { width: size, height: size, fontSize: Math.round(size * 0.38) };
  if (version && broken !== version) {
    return <img className="avatar" src={api.avatarUrl(version)} alt="" width={size} height={size}
                style={box} onError={() => setBroken(version)} />;
  }
  return <span className="avatar" style={box} aria-hidden>{initials(displayName(user))}</span>;
}

/** The GhostFetch loading state: the mark floats and its data lines stream.
 *  `fullscreen` is the app-level boot screen; inline is for a panel. */
export function GhostLoader({ label = "Loading…", fullscreen = false }: { label?: string; fullscreen?: boolean }) {
  return (
    <div className={`loader${fullscreen ? " fullscreen" : ""}`} role="status" aria-live="polite">
      <GhostMark className="ghost" size={fullscreen ? 96 : 40} animated />
      <span className="label">{label}</span>
    </div>
  );
}

/** What a screen shows when there is genuinely nothing to show yet. */
export function EmptyState({
  title, children, action, compact = false,
}: { title: string; children?: React.ReactNode; action?: React.ReactNode; compact?: boolean }) {
  return (
    <div className={`empty${compact ? " compact" : ""}`}>
      <GhostMark className="ghost" size={compact ? 52 : 72} />
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {action ? <div className="actions">{action}</div> : null}
    </div>
  );
}

/** Title, one line of explanation, and the screen's own actions on the right. */
export function PageHeader({
  title, sub, actions, back,
}: { title: React.ReactNode; sub?: React.ReactNode; actions?: React.ReactNode; back?: React.ReactNode }) {
  return (
    <div className="page-head">
      <div style={{ minWidth: 0 }}>
        {back}
        <h1>{title}</h1>
        {sub ? <p className="sub">{sub}</p> : null}
      </div>
      {actions ? <div className="actions">{actions}</div> : null}
    </div>
  );
}

export function StatCard({
  label, value, hint, tone = "muted",
}: { label: string; value: React.ReactNode; hint?: React.ReactNode; tone?: "run" | "ok" | "bad" | "muted" }) {
  return (
    <div className={`card stat tone-${tone}`}>
      <div className="label"><span className="swatch" aria-hidden />{label}</div>
      <div className="value">{value}</div>
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

// --- theme ------------------------------------------------------------------
// "system" follows prefers-color-scheme (pure CSS); light/dark pin it via
// data-theme on <html>. Applied from main.tsx before the first render, because
// the page's CSP refuses inline scripts. Storage can be unavailable (private
// windows, blocked site data), so every access is guarded and the console
// works the same without it.

export type Theme = "system" | "light" | "dark";
const THEME_KEY = "ghostfetch.theme";

export function storedTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === "light" || t === "dark" ? t : "system";
  } catch { return "system"; }
}

export function applyTheme(t: Theme) {
  const root = document.documentElement;
  if (t === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", t);
}

const THEME_EVENT = "ghostfetch-theme";

function saveTheme(t: Theme) {
  applyTheme(t);
  try { localStorage.setItem(THEME_KEY, t); } catch { /* not remembered; still applied */ }
  // Every switch on the page (sidebar, top bar) follows the same choice.
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: t }));
}

/** The chosen theme, kept in step across every control that shows it. */
function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(storedTheme);
  useEffect(() => {
    const on = (e: Event) => setTheme((e as CustomEvent<Theme>).detail);
    window.addEventListener(THEME_EVENT, on);
    return () => window.removeEventListener(THEME_EVENT, on);
  }, []);
  return [theme, saveTheme];
}

/** Light / Dark / Auto, labelled, so the choice is visible rather than hidden
 *  behind an icon that cycles. "Auto" follows the operating system. */
export function ThemeSwitch({ compact = false }: { compact?: boolean }) {
  const [theme, set] = useTheme();
  const options: [Theme, string, React.ReactNode][] = [
    ["light", "Light", <IconSun size={15} />],
    ["dark", "Dark", <IconMoon size={15} />],
    ["system", "Auto", <IconMonitor size={15} />],
  ];
  return (
    <div className={`theme-switch${compact ? " compact" : ""}`} role="radiogroup" aria-label="Colour theme">
      {options.map(([value, label, icon]) => (
        <button key={value} type="button" role="radio" aria-checked={theme === value}
                title={value === "system" ? "Follow your system setting" : `${label} mode`}
                onClick={() => set(value)}>
          {icon}{compact ? null : <span>{label}</span>}
        </button>
      ))}
    </div>
  );
}

/** One tap between light and dark, for the mobile top bar. From "Auto" it
 *  flips whatever the system is currently showing. */
export function ThemeQuickToggle() {
  const [theme, set] = useTheme();
  const systemDark = typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = theme === "dark" || (theme === "system" && systemDark);
  return (
    <button className="icon-btn" type="button" onClick={() => set(dark ? "light" : "dark")}
            aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
            title={dark ? "Switch to light mode" : "Switch to dark mode"}>
      {dark ? <IconSun /> : <IconMoon />}
    </button>
  );
}

export function Banner({ kind, children }: { kind: "bad" | "ok" | "warn"; children: React.ReactNode }) {
  return <div className={`banner ${kind}`}>{children}</div>;
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <Banner kind="bad">
      <strong>{e.error ?? "Something went wrong"}</strong>
      {e.detail ? <> — {e.detail}</> : null}
    </Banner>
  );
}

/** Poll while `active` says to, PAUSING while the tab is hidden -- a drawer of
 *  background tabs each polling a run is load nobody asked for.
 *
 *  Two things the naive version gets wrong, both of which leave a screen stuck:
 *
 *  IT FIRES ONCE IMMEDIATELY, before any visibility check. A screen that mounts
 *  while the tab is hidden otherwise never fetches its first state at all and
 *  sits on its placeholder for ever -- "Starting the browser..." over a browser
 *  that started thirty seconds ago. Measured: some embedded webviews report
 *  hidden even while displayed, so this is not only about background tabs.
 *
 *  AND IT CATCHES UP ON RETURN rather than waiting out the interval, so a tab
 *  brought back to the front is correct immediately instead of up to `ms` later.
 */
export function usePolling(
  fn: () => void,
  ms: number,
  active: boolean,
  evenWhenHidden = false,
) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!active) return;
    let timer: number;
    const tick = () => {
      if (evenWhenHidden || !document.hidden) saved.current();
      timer = window.setTimeout(tick, ms);
    };
    saved.current();          // the first read is never skipped
    timer = window.setTimeout(tick, ms);

    const onVisible = () => { if (!document.hidden) saved.current(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [ms, active, evenWhenHidden]);
}

/** Load once, with the three states every screen needs. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  const run = () => {
    setLoading(true);
    load()
      .then((d) => { setData(d); setError(null); })
      .catch(setError)
      .finally(() => setLoading(false));
  };
  useEffect(run, deps);
  return { data, error, loading, reload: run, setData };
}

export function fmtTime(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function fmtDuration(seconds: number | null) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m ${Math.round(seconds % 60)}s`;
}

export function fmtNumber(n: unknown) {
  return typeof n === "number" ? n.toLocaleString() : "—";
}

export function fmtBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
