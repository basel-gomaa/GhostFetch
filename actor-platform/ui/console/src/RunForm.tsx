import { useMemo, useState } from "react";
import { api, Actor, ActorLimits, ApiError, JsonSchema, RunOptions, SocialAccount } from "./api";
import { Banner, EmptyState, ErrorBanner, GhostLoader, PageHeader, useLoad } from "./ui";
import { BrandName } from "./brand/PlatformLogo";

/**
 * The run form, BUILT FROM THE ACTOR'S OWN input_schema.
 *
 * Not hand-written per Actor, and that is the point: the schema is what the API
 * validates against, so a form generated from it cannot drift into offering a
 * field the server will reject. Adding a field to an Actor makes it appear here
 * with no frontend change -- which is what makes the next six platforms cheap.
 *
 * Every option is a FIELD, chosen in any order. The CLI asks these as a fixed
 * sequence of questions; a form does not have to, and answering them out of
 * order is most of why a person wants a screen instead of a prompt.
 */

const HIDDEN = new Set(["ai_prompt", "analyse"]); // rendered on their own, below

function titleise(name: string) {
  return name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function Field({
  name, schema, value, onChange, disabled,
}: {
  name: string; schema: JsonSchema & { enum?: string[]; description?: string };
  value: any; onChange: (v: any) => void; disabled?: boolean;
}) {
  const id = `f-${name}`;
  const common = { id, disabled, value: value ?? "", onChange: (e: any) => onChange(e.target.value) };

  let control: React.ReactNode;
  if (schema.enum) {
    control = (
      <select {...common} onChange={(e) => onChange(e.target.value || undefined)}>
        <option value="">(default)</option>
        {schema.enum.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  } else if (schema.type === "boolean") {
    // An untouched box shows the Actor's own default, since an absent field IS
    // that default on the server -- otherwise a default-on option looks off.
    control = (
      <label className="row" style={{ gap: 8, fontWeight: 400 }}>
        <input type="checkbox" style={{ width: "auto" }} disabled={disabled}
               checked={!!(value ?? schema.default)}
               onChange={(e) => onChange(e.target.checked)} />
        <span className="muted">{schema.description}</span>
      </label>
    );
  } else if (schema.type === "integer") {
    control = (
      <input {...common} type="number" min={schema.minimum} max={schema.maximum}
             placeholder="(default)"
             onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))} />
    );
  } else if (schema.pattern?.includes("\\d{4}")) {
    // The date fields declare themselves through their pattern rather than
    // being recognised by name, so a new date field gets a date picker free.
    control = <input {...common} type="date" onChange={(e) => onChange(e.target.value || undefined)} />;
  } else {
    control = <input {...common} onChange={(e) => onChange(e.target.value || undefined)} />;
  }

  return (
    <div>
      <label htmlFor={id}>{titleise(name)}</label>
      {control}
      {schema.type !== "boolean" && schema.description ? (
        <div className="hint">{schema.description}</div>
      ) : null}
    </div>
  );
}

function fmtMinutes(m: number | null | undefined) {
  if (!m) return "no limit";
  return m % 60 === 0 ? `${m / 60} h` : `${m} min`;
}

function fmtMb(mb: number | null | undefined) {
  if (!mb) return "no limit";
  return mb >= 1024 && mb % 256 === 0 ? `${mb / 1024} GB` : `${mb} MB`;
}

/**
 * "Run options": how long this run may take and how big it may get.
 *
 * Pre-filled with the Actor's DEFAULTS (shown as placeholders, so an untouched
 * field sends nothing and the server's default applies) and bounded by its
 * MAXIMUM. Collapsed by default -- most runs never need it; the one that does
 * is a long date window, which is exactly when the timeout matters.
 */
function RunOptionsCard({
  limits, value, onChange,
}: { limits?: ActorLimits; value: RunOptions; onChange: (v: RunOptions) => void }) {
  if (!limits) return null;
  const t = limits.timeout_minutes, m = limits.memory_mb, c = limits.cpus;
  const set = (key: keyof RunOptions) => (e: React.ChangeEvent<HTMLInputElement>) =>
    onChange({ ...value, [key]: e.target.value === "" ? undefined : Number(e.target.value) });
  const summary = [
    fmtMinutes(value.timeout_minutes ?? t.default),
    fmtMb(value.memory_mb ?? m.default),
    (value.cpus ?? c.default) ? `${value.cpus ?? c.default} CPU` : "no CPU limit",
  ].join(" · ");
  return (
    <details className="card">
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>
        Run options <span className="muted" style={{ fontWeight: 400 }}>— {summary}</span>
      </summary>
      <div className="stack" style={{ marginTop: 12 }}>
        <div>
          <label htmlFor="opt-timeout">Timeout (minutes)</label>
          <input id="opt-timeout" type="number" min={1} max={t.max ?? undefined}
                 placeholder={t.default ? String(t.default) : "no limit"}
                 value={value.timeout_minutes ?? ""} onChange={set("timeout_minutes")} />
          <div className="hint">
            The run is stopped once it has been running this long. Default {fmtMinutes(t.default)}
            {t.max ? `, up to ${fmtMinutes(t.max)}` : ""}. Raise it for a long date range.
          </div>
        </div>
        <div>
          <label htmlFor="opt-memory">Memory (MB)</label>
          <input id="opt-memory" type="number" min={64} step={256} max={m.max ?? undefined}
                 placeholder={m.default ? String(m.default) : "no limit"}
                 value={value.memory_mb ?? ""} onChange={set("memory_mb")} />
          <div className="hint">
            Default {fmtMb(m.default)}{m.max ? `, up to ${fmtMb(m.max)}` : ""}. A run that
            needs more is stopped and says it ran out of memory.
          </div>
        </div>
        <div>
          <label htmlFor="opt-cpus">CPUs</label>
          <input id="opt-cpus" type="number" min={0.1} step={0.5} max={c.max ?? undefined}
                 placeholder={c.default ? String(c.default) : "no limit"}
                 value={value.cpus ?? ""} onChange={set("cpus")} />
          <div className="hint">
            Default {c.default ?? "no limit"}{c.max ? `, up to ${c.max}` : ""}.
          </div>
        </div>
      </div>
    </details>
  );
}

export function RunForm({ onStarted }: { onStarted: (runId: string) => void }) {
  const actors = useLoad<Actor[]>(() => api.actors(), []);
  const accounts = useLoad<SocialAccount[]>(() => api.accounts(), []);
  const [chosen, setChosen] = useState<string>("");
  const [values, setValues] = useState<Record<string, any>>({});
  const [prompt, setPrompt] = useState("");
  const [analyse, setAnalyse] = useState(true);
  const [options, setOptions] = useState<RunOptions>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Filtered BEFORE the `?? [0]` default below, not just before the <option>
  // list: the fallback selects the first Actor alphabetically, so filtering only
  // the rendered list would leave the form defaulted to an Actor the user cannot
  // see -- submitting a hidden test fixture from a form that looks like it says
  // something else.
  const offered = useMemo(
    () => (actors.data ?? []).filter((a) => !a.hidden),
    [actors.data],
  );

  const actor = useMemo(
    () => offered.find((a) => a.name === chosen) ?? offered[0],
    [offered, chosen],
  );
  const schema = actor?.input_schema;
  const props = schema?.properties ?? {};
  const required = new Set(schema?.required ?? []);

  // The count and the date range bound the SAME thing, and the schema says so
  // in prose ("OMIT IT when using a date range"). A form can enforce it.
  const dated = !!(values.start_date || values.end_date);
  const counted = values.max_posts != null && values.max_posts !== "";

  // Splitting a window into concurrent date chunks needs a server-side date
  // SEEK, and only a Page has one: every chunk of a group would restart its
  // walk from today, so the wall clock is unchanged and only the request load
  // multiplies. collect_page_posts refuses to chunk a group for that reason,
  // and the field silently did nothing rather than saying so.
  //
  // This mirrors is_group_url(resolve_page_url(target)) EXACTLY, which is the
  // test the collector actually applies -- deliberately not the input schema's
  // looser prose ("a numeric group id"). That form is normalised by the CLI's
  // --group flag, which this path does not go through: an actor target of
  // 123456789012345 resolves to facebook.com/<id> and is scraped as a PAGE.
  // Greying the field out for a bare number would therefore be wrong.
  const targeted = String(values.target ?? "").trim();
  const isGroup = /facebook\.com\/groups\/[^/?#]+/.test(
    /^https?:\/\//.test(targeted)
      ? targeted
      : `https://www.facebook.com/${targeted.replace(/^\/+|\/+$/g, "")}`,
  );

  // A field an Actor cannot currently honour is greyed rather than hidden, so
  // the reason is visible where the question is asked, and its value is
  // dropped on submit. No field carries the flag today; it was last used to
  // retire one Actor's comment fields, which have since come back.
  //
  // Driven by the schema's own `x-unavailable`, not by a hardcoded actor name:
  // the API owns what an actor offers (see the no-site-names rule in the
  // platform tests), and greying on `name === "tiktok"` here would put that
  // fact in two places that can disagree. Any actor may retire a field the same
  // way, and the flag's value IS the explanation shown under it -- so a field
  // can never be greyed without saying why.
  const unavailable = (name: string): string | null =>
    (props as any)[name]?.["x-unavailable"] ?? null;

  const submit = async () => {
    if (!actor) return;
    setBusy(true);
    setError(null);
    const input: Record<string, any> = {};
    for (const [k, v] of Object.entries(values)) {
      // DROPPED, not merely greyed: a value typed before the target became a
      // group is still in `values`, and sending it would put a number in
      // input.json that the run visibly does not honour.
      const ignored = (dated && k === "max_posts")
        || (isGroup && k === "page_concurrency")
        || !!unavailable(k);
      if (v !== undefined && v !== "" && !ignored) input[k] = v;
    }
    input.analyse = analyse;
    if (analyse && prompt.trim()) input.ai_prompt = prompt.trim();
    try {
      const { run_id } = await api.startRun(actor.name, input, options);
      onStarted(run_id);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const head = (
    <PageHeader title="New run"
                sub="Choose a scraper, tell it what to collect, and GhostFetch runs it in its own isolated container." />
  );
  if (actors.loading) return <div className="stack">{head}<GhostLoader label="Loading scrapers…" /></div>;
  if (actors.error) return <div className="stack">{head}<ErrorBanner error={actors.error} /></div>;
  if (!actor)
    return (
      <div className="stack">
        {head}
        <EmptyState title="No scrapers available">
          {actors.data?.length
            ? "No scrapers are available. The platform has Actors installed, but all of them are hidden."
            : "No actors are installed."}
        </EmptyState>
      </div>
    );

  const err = error as ApiError | null;
  const needsAccount = err?.status === 409 && (err.body as any)?.reconnect;
  const connected = accounts.data?.find((a) => a.status === "connected");

  return (
    <div className="stack">
      {head}
      {needsAccount ? (
        <Banner kind="warn">
          <strong>{err!.error}</strong> — {err!.detail}{" "}
          <a href="#/accounts">Connect an account →</a>
        </Banner>
      ) : (
        <ErrorBanner error={error} />
      )}

      {!connected && accounts.data && accounts.data.length === 0 ? (
        <Banner kind="warn">
          You have no connected account yet. A scrape needs one.{" "}
          <a href="#/accounts">Connect one →</a>
        </Banner>
      ) : null}

      <div className="card stack">
        <div>
          <label id="actor-label">Scraper</label>
          {/* One card per scraper, each with its logo. Choosing one is the same
              act the old dropdown was: switch the Actor and clear the form. */}
          <div className="picker" role="radiogroup" aria-labelledby="actor-label">
            {offered.map((a) => (
              <button key={a.name} type="button" role="radio" className="picker-option"
                      aria-checked={a.name === actor.name}
                      onClick={() => { if (a.name !== actor.name) { setChosen(a.name); setValues({}); setOptions({}); } }}>
                <BrandName icon={a.icon} label={a.label} name={a.name} size={30} strong={false} />
              </button>
            ))}
          </div>
          {actor.description ? <div className="hint">{actor.description}</div> : null}
        </div>

        {Object.entries(props)
          .filter(([name]) => !HIDDEN.has(name))
          .map(([name, s]) => (
            <div key={name}>
              <Field
                name={name + (required.has(name) ? " *" : "")}
                schema={s}
                value={values[name]}
                disabled={(dated && name === "max_posts")
                          || (isGroup && name === "page_concurrency")
                          || !!unavailable(name)}
                onChange={(v) => setValues((prev) => ({ ...prev, [name]: v }))}
              />
              {name === "max_posts" && dated ? (
                <div className="hint">
                  Ignored while a date range is set — the range is the stopping
                  condition, and a count here would cut it short.
                </div>
              ) : null}
              {unavailable(name) ? (
                <div className="hint">{unavailable(name)}</div>
              ) : null}
              {name === "page_concurrency" && isGroup ? (
                <div className="hint">
                  Ignored for a group — groups have no server-side date seek, so
                  every worker would repeat the same walk from today.
                </div>
              ) : null}
              {name === "end_date" && counted && !dated ? null : null}
            </div>
          ))}
      </div>

      <RunOptionsCard limits={actor.limits} value={options} onChange={setOptions} />

      <div className="card stack">
        <label className="row" style={{ gap: 8, fontWeight: 600 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={analyse}
                 onChange={(e) => setAnalyse(e.target.checked)} />
          Analyse the results and produce a report
        </label>
        <div>
          <label htmlFor="prompt">Your analysis instructions</label>
          <textarea id="prompt" value={prompt} disabled={!analyse}
                    placeholder="e.g. Focus on complaints about delivery times, and quote the strongest examples."
                    onChange={(e) => setPrompt(e.target.value)} />
          <div className="hint">
            Added to the built-in brief rather than replacing it, so the report
            keeps its structure and always renders. Leave empty for the standard
            analysis.
          </div>
        </div>
      </div>

      <div className="row">
        <button className="primary" onClick={submit}
                disabled={busy || !values[[...required][0] ?? "target"]}>
          {busy ? <span className="spin" /> : null}
          {busy ? "Starting…" : "Start run"}
        </button>
        <span className="muted">
          {dated ? "Collecting the whole date range." : counted ? "Collecting a fixed number of posts." : ""}
        </span>
      </div>
    </div>
  );
}
