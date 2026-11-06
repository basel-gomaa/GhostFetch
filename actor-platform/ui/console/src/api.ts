/**
 * The one place that talks to the API.
 *
 * AUTHENTICATION IS THE SESSION COOKIE, and nothing here ever sees it: it is
 * HttpOnly, so this code cannot read it and neither can an injected script.
 * The console holds no token, which is why there is nothing here to leak.
 *
 * `Sec-Fetch-Site` is set by the BROWSER, not by us, and the API refuses any
 * cookie-authenticated write that does not carry a same-origin value. That is
 * the whole CSRF story from this side: there is no token to thread through,
 * and no way for this file to accidentally weaken it.
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly error: string,
    readonly detail: string,
    readonly body: Record<string, unknown> = {},
  ) {
    super(detail || error);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    // Same-origin, so the cookie rides along without CORS being involved.
    credentials: "same-origin",
    headers: {
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...(init.headers ?? {}),
    },
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let parsed: any = undefined;
  try {
    parsed = text ? JSON.parse(text) : undefined;
  } catch {
    parsed = undefined;
  }

  if (!response.ok) {
    // The platform answers every failure as {error, detail}. FastAPI's own
    // validation shape ({detail: [...]}) can still surface for a malformed
    // request, so both are handled rather than assumed away.
    const detail =
      typeof parsed?.detail === "string"
        ? parsed.detail
        : Array.isArray(parsed?.detail)
          ? parsed.detail.map((d: any) => d.msg).join("; ")
          : text.slice(0, 300);
    throw new ApiError(response.status, parsed?.error ?? "request failed", detail, parsed ?? {});
  }

  return (parsed ?? (text as unknown)) as T;
}

const get = <T,>(path: string) => request<T>(path);
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const del = <T,>(path: string) => request<T>(path, { method: "DELETE" });

// --- types ------------------------------------------------------------------

export interface User {
  id: string; email: string; username: string; is_admin: boolean;
  // The editable profile. display_name is what the person chose to be called
  // (null: show the username, which is what they sign in with and never
  // changes). avatar_version changes with the photo and is null when there is
  // none; it is the cache key in avatarUrl().
  display_name?: string | null;
  avatar_version?: string | null;
}

export interface SocialAccount {
  platform: string;
  status: string;
  external_id: string | null;
  connected_at: string | null;
  last_verified_at: string | null;
  expired_reason: string | null;
  needs_reconnect: boolean;
}

/** A logo, as config/actors.json declares it: ONE SVG path on a 24x24 grid
 *  and an optional brand colour (null: draw in the text colour, which is how a
 *  black logo stays visible in dark mode). Drawing data only, never markup. */
export interface BrandIcon { d: string; color: string | null }

export interface PlatformOption {
  name: string;
  // How to show it, from config. The console names no site itself.
  label?: string | null;
  icon?: BrandIcon | null;
  // false when the site serves its public data anonymously: there is no session
  // to capture, so `note` says why rather than a Connect button pretending.
  connectable: boolean;
  note?: string | null;
}

export interface LoginSession { id: string; platform: string; expires_at: string; demo?: boolean }
export interface LoginStatus {
  id: string; status: string; ready: boolean; signed_in: boolean;
  cookie_names: string[]; expires_at: string | null;
}

export interface JsonSchema {
  type?: string;
  properties?: Record<string, JsonSchema & { description?: string; enum?: string[]; maxLength?: number }>;
  required?: string[];
  description?: string;
  minimum?: number;
  maximum?: number;
  pattern?: string;
  /** What the Actor assumes when the field is absent; a form shows it. */
  default?: unknown;
}

export interface Actor {
  name: string; description?: string; input_schema?: JsonSchema;
  // How to show it to a person -- its label and logo -- declared in
  // config/actors.json. Null for an Actor that declares none (the fixtures).
  label?: string | null;
  icon?: BrandIcon | null;
  // Presentation only. A hidden Actor is still listed here and still runs --
  // the platform's own test fixtures are hidden and every suite runs them --
  // so this is a "do not offer it", never a "cannot use it".
  hidden?: boolean;
  // Where a person solves a challenge this Actor parks on: true means a browser
  // on their OWN machine, false means one inside the container (visible only
  // through a streamed display, if it exports one). Declared in
  // config/actors.json -- never inferred from the Actor's name.
  drives_host_browser?: boolean;
  // Whether this Actor ever PARKS waiting for a person. Only such an Actor may
  // be offered a "waiting for you" prompt or a resume control -- for any other,
  // the prompt describes a run that is not waiting and the control resumes
  // nothing. Declared in config/actors.json, never inferred from the name.
  awaits_human?: boolean;
  // Per-run limits this Actor offers: a default and a maximum for each, from
  // config/actors.json. Null means unlimited. A run may ask for any value up to
  // the maximum; the server refuses anything above it rather than lowering it.
  limits?: ActorLimits;
}

export interface LimitRange { default: number | null; max: number | null }
export interface ActorLimits {
  timeout_minutes: LimitRange;
  memory_mb: LimitRange;
  cpus: LimitRange;
}

/** What one run asks for; anything left out takes the Actor's default. */
export interface RunOptions { timeout_minutes?: number; memory_mb?: number; cpus?: number }

/** Why a run failed, in the shape shared/errors.py defines.
 *
 * `code` is the category a machine acts on and `message` the sentence a person
 * reads; `detail` is the narrower code the Actor or Docker gave, kept so that
 * mapping into a category never destroys the more specific thing that was
 * known. Every field but the first two is optional, because a failure that was
 * classified without a stage or a retry hint is still a classified failure.
 */
export interface RunError {
  code: string;
  message: string;
  retryable: boolean;
  retry_after_seconds?: number | null;
  stage?: string | null;
  actor?: string | null;
  attempt?: number | null;
  detail?: string | null;
}

export interface RunSummary {
  run_id: string; actor: string; status: string;
  created_at: string; started_at: string | null; finished_at: string | null;
  duration_seconds: number | null; exit_code: number | null; error: string | null;
  attempts?: number;
  // ADDED beside `error`, never replacing it. A run that failed before this
  // existed, or whose failure could not be classified into anything better
  // than the sentence `error` already carried, has none -- so every render
  // path has to keep working without it.
  error_info?: RunError | null;
  // The run failed and still produced usable data. The results below it are
  // worth reading rather than being the empty shell a failure usually implies.
  partial?: boolean;
  // The limits this run ran under (what the Worker enforced), and the highest
  // memory use it saw. Null limits mean unlimited; a null peak means the run
  // was too short to sample, or predates sampling.
  timeout_s?: number | null;
  memory_mb?: number | null;
  cpus?: number | null;
  peak_memory_mb?: number | null;
}

/** What a run is waiting for a person to do. Null when it is waiting for nobody. */
export interface Attention {
  needs: string;
  reason: string;
  message: string;
  // WHICH challenge of this run this is, counted from 1. The browser panel is
  // keyed on it, because "I've solved it" is a hint the Actor re-checks -- so
  // the same CAPTCHA can announce itself again moments after being dismissed,
  // and only a changed identity should reopen the panel.
  // Optional: a run started before the Actor carried one has none.
  challenge?: number;
  since: string;
  expires_at: string;
}

export interface Artifact { name: string; bytes: number }

// --- user administration (api/admin.py) --------------------------------------
// Every route is admin-only ON THE SERVER. The console hides the screen from a
// non-admin for tidiness; that is presentation, never the access check.

export interface AdminUser {
  id: string; email: string; username: string; display_name?: string | null;
  is_admin: boolean; disabled: boolean;
  disabled_at: string | null; created_at: string | null;
  // Temporarily locked out after too many failed sign-ins. Clears by itself,
  // or an admin can unlock it early.
  locked?: boolean;
}

export interface AdminUserDetail extends AdminUser {
  live_sessions: number; api_keys: number; runs: number;
}

/** The user after a change. `already` is true when it was already in that
 *  state: every change is idempotent, as cancelling a cancelled run is. */
export interface AdminChange { user: AdminUser; already: boolean }

/** What disabling actually stopped -- reported by the server, not inferred. */
export interface DisableResult extends AdminChange {
  sessions_revoked: boolean;
  runs_cancelled: number;
  runs_already_finished: number;
  login_browsers_stopped: number;
  // Closed in the database, but the container could not be confirmed stopped.
  login_browsers_unconfirmed: number;
}

export type AdminAction = "promote" | "demote" | "disable" | "enable" | "unlock";

// --- invite-only signup (api/invites.py) ---------------------------------------
export interface SignupOptions {
  signup: "invite" | "open" | "closed";
  invite_required: boolean;
}
export interface Invite {
  id: string;
  note: string | null;
  status: "pending" | "used" | "revoked" | "expired";
  created_at: string | null;
  expires_at: string | null;
  used_at: string | null;
  created_by: string | null;   // username; null for the operator key
  used_by: string | null;
}
// Returned once, on creation. Only its hash is stored on the server.
export interface NewInvite extends Invite { token: string }

export const api = {
  signup: (email: string, username: string, password: string, invite?: string) =>
    post<User>("/auth/signup", { email, username, password, ...(invite ? { invite } : {}) }),
  signupOptions: () => get<SignupOptions>("/auth/signup-options"),
  login: (identifier: string, password: string) =>
    post<User>("/auth/login", { identifier, password }),
  logout: () => post<void>("/auth/logout"),
  me: () => get<User>("/auth/me"),

  platforms: () => get<PlatformOption[]>("/social/platforms"),
  accounts: () => get<SocialAccount[]>("/social/accounts"),
  disconnect: (platform: string) => del<void>(`/social/accounts/${platform}`),
  startLogin: (platform: string) => post<LoginSession>("/social/login", { platform }),
  loginStatus: (id: string) => get<LoginStatus>(`/social/login/${id}`),
  capture: (id: string) => post<SocialAccount>(`/social/login/${id}/capture`),
  closeLogin: (id: string) => del<void>(`/social/login/${id}`),

  actors: () => get<{ actors: Actor[] }>("/actors").then((r) => r.actors),
  // Limits travel as QUERY parameters, never in the body: the body is the
  // Actor's input, validated against the Actor's own schema.
  startRun: (actor: string, input: unknown, options: RunOptions = {}) => {
    const query = new URLSearchParams();
    for (const [k, v] of Object.entries(options)) {
      if (v !== undefined && v !== null && !Number.isNaN(v)) query.set(k, String(v));
    }
    const qs = query.toString();
    return post<{ run_id: string }>(`/actors/${actor}/runs${qs ? `?${qs}` : ""}`, input);
  },
  runs: (limit = 50) => get<{ runs: RunSummary[] }>(`/runs?limit=${limit}`).then((r) => r.runs),
  run: (id: string) => get<RunSummary>(`/runs/${id}`),
  runInput: (id: string) => get<any>(`/runs/${id}/input`),
  runOutput: (id: string) => get<any>(`/runs/${id}/output`),
  runLogs: (id: string) => request<string>(`/runs/${id}/logs`),
  // Items the Actor streamed, readable WHILE the run goes. Deliberately never
  // filtered by `?collection=` here: collection names such as "data#1" carry a
  // "#", which starts a URL fragment, so an unencoded filter would silently
  // arrive as collection=data. The caller pages with `after` and groups itself.
  runItems: (id: string, after = 0, limit = 1000) =>
    get<{ items: RunItem[]; next_after: number | null }>(
      `/runs/${id}/items?after=${after}&limit=${limit}`),
  cancelRun: (id: string) => post<{ status: string }>(`/runs/${id}/cancel`),
  // Removes the run and everything it produced. Finished runs only (409 otherwise).
  deleteRun: (id: string) => del<void>(`/runs/${id}`),
  runAttention: (id: string) => get<Attention | null>(`/runs/${id}/attention`),
  resumeRun: (id: string) => post<{ status: string }>(`/runs/${id}/resume`),
  artifacts: (id: string) =>
    get<{ artifacts: Artifact[] }>(`/runs/${id}/artifacts`).then((r) => r.artifacts),
  artifactUrl: (id: string, name: string) => `/runs/${id}/artifacts/${encodeURIComponent(name)}`,

  updateProfile: (display_name: string | null) =>
    request<User>("/me/profile", { method: "PATCH", body: JSON.stringify({ display_name }) }),
  // The image is sent as raw bytes. The server reads its type from the bytes,
  // so the header is a courtesy, not the check.
  uploadAvatar: (image: Blob) =>
    request<User>("/me/avatar", {
      method: "PUT", body: image, headers: { "content-type": image.type || "application/octet-stream" },
    }),
  deleteAvatar: () => del<User>("/me/avatar"),
  avatarUrl: (version: string) => `/me/avatar?v=${encodeURIComponent(version)}`,

  apiKeys: () => get<any[]>("/me/api-keys"),
  createApiKey: (label: string) => post<any>("/me/api-keys", { label }),
  revokeApiKey: (id: string) => del<void>(`/me/api-keys/${id}`),

  adminUsers: (q = "") =>
    get<{ users: AdminUser[] }>(
      `/admin/users?limit=500${q ? `&q=${encodeURIComponent(q)}` : ""}`).then((r) => r.users),
  adminUser: (id: string) => get<AdminUserDetail>(`/admin/users/${encodeURIComponent(id)}`),
  adminAction: <T extends AdminChange = AdminChange>(id: string, action: AdminAction) =>
    post<T>(`/admin/users/${encodeURIComponent(id)}/${action}`),
  adminInvites: () =>
    get<{ invites: Invite[]; signup: SignupOptions["signup"] }>("/admin/invites?limit=200"),
  createInvite: (note: string, days?: number) =>
    post<NewInvite>("/admin/invites", { ...(note ? { note } : {}), ...(days ? { days } : {}) }),
  revokeInvite: (id: string) => del<Invite>(`/admin/invites/${encodeURIComponent(id)}`),
};

/** The link an invitee opens. The token rides in the FRAGMENT (#invite=...),
 *  which browsers never send to the server, so it stays out of every server
 *  log. Auth.tsx reads it once and removes it from the address bar. The API
 *  serves this console at "/" (api/main.py). */
export const inviteLink = (token: string) =>
  `${window.location.origin}/#invite=${encodeURIComponent(token)}`;

/** One item an Actor appended to its dataset (GET /runs/{id}/items). */
export interface RunItem {
  seq: number;
  collection: string;
  key: string | null;
  data: any;
}

export const TERMINAL = new Set(["SUCCEEDED", "FAILED", "PLATFORM_ERROR", "CANCELLED"]);
