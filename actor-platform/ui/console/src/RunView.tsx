import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { api, Artifact, RunError, RunItem, RunSummary, TERMINAL } from "./api";
import { Badge, Banner, EmptyState, ErrorBanner, GhostLoader, fmtBytes, fmtDuration, fmtNumber, fmtTime, useLoad, usePolling } from "./ui";
import { IconArrowLeft, IconFile } from "./brand/icons";
import { ActorName } from "./brand/PlatformLogo";
import { countThreads, CSV_BOM, toThreadCsv, toThreadJson } from "./exportCsv";

/** One scraped post, flattened out of whatever the actor produced. */
interface Row {
  date: string; caption: string; likes: number; comments: number;
  shares: number; type: string; url: string;
  /** The post's own collected comments (replies nested), when the run has them. */
  thread: any[];
  /** The post exactly as the Actor produced it, for a lossless JSON export. */
  raw: any;
}

/** What a person should DO about this failure, keyed on the category.
 *
 * KEYED ON THE CATEGORY, never on the actor -- the console names no site, the
 * same rule the browser-panel advice and the greyed input fields both follow.
 * That is also what makes this correct for an Actor added later: a new
 * collector reporting AUTH_ERROR gets the right sentence with no change here.
 *
 * The distinction every line is drawing is whose problem this is. A person
 * arrives at a failed run asking "is this mine to fix?", and the category is
 * the only thing that answers it -- which is precisely what a single free-text
 * line could not do.
 */
function failureAdvice(error: RunError): string {
  switch (error.code) {
    case "INPUT_ERROR":
      return "Check the run's input and start a new run — retrying this one would fail the same way.";
    case "AUTH_ERROR":
      return "Reconnect this platform's account, then start a new run.";
    case "ACCESS_ERROR":
      return "The target could not be read as asked — it may be private, removed, or restricted. Retrying will not change that.";
    case "RATE_LIMITED":
      return error.retry_after_seconds
        ? `The platform asked us to slow down. Try again in about ${Math.ceil(error.retry_after_seconds)}s.`
        : "The platform asked us to slow down. Try again later.";
    case "TRANSIENT_ERROR":
      return "A temporary fault. Starting the run again is likely to succeed.";
    case "PLATFORM_ERROR":
      return "This failed on our side, not at the target. Nothing about the input needs changing.";
    case "ACTOR_ERROR":
      return "The collector failed while handling the response. The logs below carry the detail.";
    case "TIMEOUT":
      return "A configured limit was exceeded before the run could finish.";
    default:
      return "This failure could not be classified. The logs below carry the detail.";
  }
}

/** The one-line footer of facts, assembled from whatever the error carries.
 *
 * Every field is optional, so this builds a list and joins it rather than
 * rendering a fixed row that would print "stage: null" for a run classified
 * without one.
 */
function failureFacts(error: RunError, attempts?: number): string {
  const facts: string[] = [];
  if (error.stage) facts.push(`stage: ${error.stage}`);
  facts.push(error.retryable ? "retrying may help" : "retrying will not help");
  if (attempts && attempts > 1) facts.push(`attempts: ${attempts}`);
  return facts.join("  ·  ");
}


/** What this platform calls a share, and which field carries it.
 *
 * The MIRROR of reports/common.py::share_terms, and it has to exist for the
 * same reason: Instagram has no shares. Its public share-equivalent is a
 * REPOST, and the collector emits it under `reposts` -- so reading `p.shares`
 * there is not merely a wrong label, it is always ABSENT and rendered as 0.
 * Measured on a live run: a post with 25,660 reposts showed "Shares 0".
 *
 * Every other platform here has a real shares metric and uses the default,
 * Threads included (its reposts are genuine shares, which is why the report
 * layer has no branch for it either).
 */
function shareTerms(platform: string): { key: string; label: string } {
  return platform === "Instagram"
    ? { key: "reposts", label: "Reposts" }
    : { key: "shares", label: "Shares" };
}

function toRows(output: any): Row[] {
  return postsToRows(output?.posts ?? [], output?.platform ?? "");
}

function postsToRows(posts: any[], platform: string): Row[] {
  const { key } = shareTerms(platform);
  return posts.map((p: any) => ({
    date: p.post_date ?? p.created_at ?? "",
    caption: p.caption ?? p.text ?? "",
    likes: p.likes ?? 0,
    comments: p.comments_count ?? (p.comments?.length ?? 0),
    // Read by the platform's OWN field name, then carried on the row under one
    // name so sorting, filtering and the export need no further branching.
    shares: p[key] ?? 0,
    type: p.content_type ?? "",
    url: p.post_url ?? "",
    thread: Array.isArray(p.comments) ? p.comments : [],
    raw: p,
  }));
}

/** BATCHES are item collections named "data#<n>" -- a generic convention of
 *  the Actor contract, not any one Actor's: an Actor that delivers a big result
 *  in pieces streams each finished piece under its own collection, and the Data
 *  tab shows them as sections WHILE the run is still going. Anything else an
 *  Actor streams (e.g. "posts", "comments") is not a batch and is left alone. */
const BATCH_RE = /^data#(\d+)$/;

interface Batch { name: string; posts: any[] }

function toBatches(items: RunItem[]): Batch[] {
  const byName = new Map<string, any[]>();
  for (const item of items) {
    if (!BATCH_RE.test(item.collection)) continue;
    if (!byName.has(item.collection)) byName.set(item.collection, []);
    byName.get(item.collection)!.push(item.data);
  }
  // Numerically, so data#10 follows data#9 rather than data#1.
  return [...byName.entries()]
    .map(([name, posts]) => ({ name, posts }))
    .sort((a, b) => Number(a.name.match(BATCH_RE)![1]) - Number(b.name.match(BATCH_RE)![1]));
}

/** A GROUP run, or any run over BATCH_SIZE posts, that was NOT streamed in
 *  batches, shown the same way: its posts in sections of BATCH_SIZE, newest
 *  first -- the order the Actor's own batches use. A smaller page run stays a
 *  single table. Undated posts go last rather than being guessed into a section. */
const BATCH_SIZE = 100;

function chunkPosts(posts: any[]): Batch[] {
  const t = (p: any) => { const v = Date.parse(p?.post_date ?? p?.created_at ?? ""); return Number.isNaN(v) ? -Infinity : v; };
  const sorted = [...posts].sort((a, b) => t(b) - t(a));
  const out: Batch[] = [];
  for (let i = 0; i < sorted.length; i += BATCH_SIZE) {
    out.push({ name: `data#${out.length + 1}`, posts: sorted.slice(i, i + BATCH_SIZE) });
  }
  return out;
}

function Comment({ c, depth }: { c: any; depth: number }) {
  const who = c.username || "(unknown)";
  const when = c.comment_date ? String(c.comment_date).slice(0, 16).replace("T", " ") : "";
  return (
    <div style={{ marginLeft: depth * 22, padding: "4px 0" }}>
      <div>
        <strong>
          {c.profile_url
            ? <a href={c.profile_url} target="_blank" rel="noreferrer noopener">{who}</a>
            : who}
        </strong>
        <span className="muted">
          {when ? ` · ${when}` : ""}
          {c.likes ? ` · ${fmtNumber(c.likes)} like(s)` : ""}
          {c.comment_url
            ? <> · <a href={c.comment_url} target="_blank" rel="noreferrer noopener">open</a></>
            : null}
        </span>
      </div>
      <div className="wrap" style={{ maxWidth: 760, whiteSpace: "pre-wrap" }}>
        {c.comment || <span className="muted">(no text)</span>}
      </div>
      {(c.reply_comments ?? []).map((r: any, i: number) => (
        <Comment key={r.comment_id ?? i} c={r} depth={depth + 1} />
      ))}
    </div>
  );
}

function download(name: string, body: string, type: string) {
  // A CSV gets a BOM so Excel reads it as UTF-8 -- see exportCsv.CSV_BOM.
  const text = type === "text/csv" ? CSV_BOM + body : body;
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  URL.revokeObjectURL(url);
}

function Dataset({ rows, shareLabel, name = "posts", sharesKey = "shares", pageName = "" }: {
  rows: Row[]; shareLabel: string;
  /** File name for the exports, e.g. "data-2" for a batch. */
  name?: string;
  /** The field this platform's share count lives in (see shareTerms). */
  sharesKey?: string;
  /** Who a post row is by, when the posts do not say (a page's own name). */
  pageName?: string;
}) {
  // Which post's comments are open, keyed by URL so re-sorting keeps it open.
  const [open, setOpen] = useState<string | null>(null);
  // NEWEST FIRST by default, not most-liked. A run is a period of time -- and a
  // dated run is one the caller chose the period for -- so "when" is the axis
  // the rows are read along; reports/facebook_report.py orders its post list
  // the same way. Opening on `likes` made a date-windowed group scrape look
  // like it had ignored the window, because the dates arrive in DISCOVERY
  // order (09-02, 09-04, 09-03, 09-01 ...) and nothing was re-ordering them.
  const [sort, setSort] = useState<{ key: keyof Row; dir: 1 | -1 }>({ key: "date", dir: -1 });
  const [filter, setFilter] = useState("");

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const matched = needle
      ? rows.filter((r) => r.caption.toLowerCase().includes(needle) || r.type.toLowerCase().includes(needle))
      : rows;
    return [...matched].sort((a, b) => {
      if (sort.key === "date") {
        // CHRONOLOGICALLY, not as text. Comparing ISO strings happens to work
        // while every post carries the same UTC offset, and silently stops
        // working for one that does not: "2026-09-02T09:00+02:00" is 07:00 UTC
        // and "2026-09-02T08:00+00:00" is 08:00 UTC, so the first is the OLDER
        // of the two -- while as text it sorts as though it were the newer.
        const x = Date.parse(a.date), y = Date.parse(b.date);
        const xb = Number.isNaN(x), yb = Number.isNaN(y);
        // Undated posts sink in BOTH directions rather than flipping to the
        // top when the arrow flips: a share's attached story carries no date,
        // and a screenful of blanks reads as a broken scrape.
        if (xb || yb) return xb && yb ? 0 : xb ? 1 : -1;
        return (x - y) * sort.dir;
      }
      const x = a[sort.key], y = b[sort.key];
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * sort.dir;
    });
  }, [rows, sort, filter]);

  const head = (key: keyof Row, label: string, cls = "") => (
    <th className={cls} onClick={() => setSort((s) => ({ key, dir: s.key === key && s.dir === -1 ? 1 : -1 }))}>
      {label}{sort.key === key ? (sort.dir === -1 ? " ↓" : " ↑") : ""}
    </th>
  );

  if (!rows.length) return <EmptyState compact title="No posts">This run collected no posts.</EmptyState>;

  return (
    <div className="stack">
      <div className="row between">
        <input className="grow" style={{ maxWidth: 320 }} placeholder="Filter captions…"
               value={filter} onChange={(e) => setFilter(e.target.value)} />
        <div className="row">
          <span className="muted">{shown.length} of {rows.length}</span>
          {/* The SHOWN posts (filter applies), each with its comment section. */}
          <button onClick={() => download(`${name}.csv`,
                                          toThreadCsv(shown.map((r) => r.raw), shareLabel, sharesKey, pageName),
                                          "text/csv")}>Export CSV</button>
          <button onClick={() => download(`${name}.json`,
                                          toThreadJson(name, shown.map((r) => r.raw), sharesKey),
                                          "application/json")}>
            Export JSON
          </button>
        </div>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              {head("date", "Date")}
              {head("caption", "Caption")}
              {head("type", "Type")}
              {head("likes", "Likes", "num")}
              {head("comments", "Comments", "num")}
              {head("shares", shareLabel, "num")}
              <th />
            </tr>
          </thead>
          <tbody>
            {shown.map((r, i) => {
              const id = r.url || String(i);
              const expandable = r.thread.length > 0;
              const isOpen = expandable && open === id;
              return [
                <tr key={id} onClick={expandable ? () => setOpen(isOpen ? null : id) : undefined}
                    style={expandable ? { cursor: "pointer" } : undefined}
                    title={expandable ? "Show this post's comments" : undefined}>
                  <td style={{ whiteSpace: "nowrap" }}>{r.date ? r.date.slice(0, 16).replace("T", " ") : "—"}</td>
                  <td className="wrap">{r.caption.slice(0, 220) || <span className="muted">(no caption)</span>}</td>
                  <td>{r.type}</td>
                  <td className="num">{fmtNumber(r.likes)}</td>
                  <td className="num">
                    {expandable ? <>{isOpen ? "▾ " : "▸ "}{fmtNumber(r.comments)}</> : fmtNumber(r.comments)}
                  </td>
                  <td className="num">{fmtNumber(r.shares)}</td>
                  <td>{r.url ? <a href={r.url} target="_blank" rel="noreferrer noopener"
                                  onClick={(e) => e.stopPropagation()}>open</a> : null}</td>
                </tr>,
                isOpen ? (
                  <tr key={`${id}::comments`}>
                    <td colSpan={7} style={{ background: "var(--panel-2)" }}>
                      <div className="muted" style={{ marginBottom: 6 }}>
                        {r.thread.length} collected comment(s)
                        {r.raw?.comment_collection?.stop_reason
                          ? ` · stopped: ${r.raw.comment_collection.stop_reason}` : ""}
                      </div>
                      {r.thread.map((c: any, j: number) => (
                        <Comment key={c.comment_id ?? j} c={c} depth={0} />
                      ))}
                    </td>
                  </tr>
                ) : null,
              ];
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** One finished batch: a header line with its counts and its two downloads,
 *  collapsed by default -- a nine-batch run is nine lines, not 900 rows. The
 *  downloads always cover the WHOLE batch, never the table's filtered view. */
function BatchSection({ batch, platform, shareLabel, pageName = "" }: {
  batch: Batch; platform: string; shareLabel: string; pageName?: string;
}) {
  const [open, setOpen] = useState(false);
  const { comments, replies } = countThreads(batch.posts);
  const file = batch.name.replace("#", "-");  // "#" is not filename-safe
  const { key } = shareTerms(platform);
  const stop = (fn: () => void) => (e: MouseEvent) => { e.stopPropagation(); fn(); };
  return (
    <div className="card" style={{ padding: 0 }}>
      <div className="row between" onClick={() => setOpen((o) => !o)}
           style={{ cursor: "pointer", padding: "10px 14px" }}
           title={open ? "Hide this batch's posts" : "Show this batch's posts"}>
        <div>
          <strong>{open ? "▾ " : "▸ "}{batch.name}</strong>
          <span className="muted">
            {` · ${fmtNumber(batch.posts.length)} posts · ${fmtNumber(comments)} comments`}
            {replies ? ` · ${fmtNumber(replies)} replies` : ""}
          </span>
        </div>
        <div className="row">
          <button onClick={stop(() => download(`${file}.csv`,
                                               toThreadCsv(batch.posts, shareLabel, key, pageName), "text/csv"))}>
            CSV
          </button>
          <button onClick={stop(() => download(`${file}.json`,
                                               toThreadJson(batch.name, batch.posts, key), "application/json"))}>
            JSON
          </button>
        </div>
      </div>
      {open ? (
        <div style={{ padding: "0 14px 14px" }}>
          <Dataset rows={postsToRows(batch.posts, platform)} shareLabel={shareLabel}
                   name={file} sharesKey={key} pageName={pageName} />
        </div>
      ) : null}
    </div>
  );
}

/** Batches STACKED IN ORDER, each appearing under the previous one as it
 *  finishes. Sections are keyed by name and only ever appended, so a new batch
 *  arriving never collapses or moves one the user has opened. */
function Batches({ batches, platform, shareLabel, active, pageName = "" }: {
  batches: Batch[]; platform: string; shareLabel: string; active: boolean; pageName?: string;
}) {
  const all = batches.flatMap((b) => b.posts);
  const { key } = shareTerms(platform);
  return (
    <div className="stack">
      <div className="row between">
        <span className="muted">
          {fmtNumber(all.length)} posts in {batches.length} batch{batches.length === 1 ? "" : "es"}
        </span>
        <div className="row">
          <span className="muted">Download all:</span>
          <button onClick={() => download("data-all.csv", toThreadCsv(all, shareLabel, key, pageName), "text/csv")}>
            CSV
          </button>
          <button onClick={() => download("data-all.json", toThreadJson("all", all, key), "application/json")}>
            JSON
          </button>
        </div>
      </div>
      {batches.map((b) => (
        <BatchSection key={b.name} batch={b} platform={platform} shareLabel={shareLabel} pageName={pageName} />
      ))}
      {active ? <p className="muted"><span className="spin" /> next batch on the way…</p> : null}
    </div>
  );
}

export function RunView({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [tab, setTab] = useState<"logs" | "data" | "report">("logs");
  const [logs, setLogs] = useState("");
  const run = useLoad<RunSummary>(() => api.run(runId), [runId]);
  const [output, setOutput] = useState<any>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  // Batch items streamed by the Actor (collections "data#<n>"), accumulated
  // incrementally: each poll asks only for items AFTER the last seq seen.
  const [batchItems, setBatchItems] = useState<RunItem[]>([]);
  const itemsAfter = useRef(0);
  const itemsRun = useRef(runId);
  const pulling = useRef(false);

  const active = !!run.data && !TERMINAL.has(run.data.status);

  // EVERY PIECE OF PER-RUN STATE IS DROPPED WHEN THE RUN CHANGES. This
  // component is reused across runs -- the same instance, a new `runId` prop --
  // so anything not reset here would go on describing the run you came FROM.
  useEffect(() => {
    setLogs("");
    setOutput(null);
    setArtifacts([]);
    setBatchItems([]);
    itemsAfter.current = 0;
    itemsRun.current = runId;
    pulling.current = false;
  }, [runId]);

  // Fetch the items appended since the last poll, following next_after until
  // caught up. One pull at a time, so a slow page cannot append twice; a reply
  // for a run we have since navigated away from is dropped.
  const pullItems = () => {
    if (pulling.current) return;
    pulling.current = true;
    const forRun = runId;
    const step = (pages: number) => {
      api.runItems(forRun, itemsAfter.current).then((page) => {
        if (itemsRun.current !== forRun) return;
        const got = page.items ?? [];
        if (got.length) {
          itemsAfter.current = got[got.length - 1].seq;
          const fresh = got.filter((i) => BATCH_RE.test(i.collection));
          if (fresh.length) setBatchItems((prev) => [...prev, ...fresh]);
        }
        if (page.next_after != null && pages < 20) step(pages + 1);
        else pulling.current = false;
      }).catch(() => { pulling.current = false; });
    };
    step(0);
  };

  // Poll only while the run is ACTIVE. A finished run's page stops asking,
  // which is the difference between a dashboard and a load generator.
  usePolling(() => {
    api.run(runId).then((r) => run.setData(r)).catch(() => {});
    api.runLogs(runId).then(setLogs).catch(() => {});
    // Batches land here while the run is still going.
    pullItems();
  }, 2000, active);

  // ...and load the finished artefacts once, when it finishes.
  usePolling(() => {}, 1e9, false);
  const settled = run.data && TERMINAL.has(run.data.status);
  useMemo(() => {
    if (!settled) return;
    api.runLogs(runId).then(setLogs).catch(() => {});
    api.runOutput(runId).then(setOutput).catch(() => setOutput(null));
    api.artifacts(runId).then(setArtifacts).catch(() => setArtifacts([]));
    // The Worker drains the dataset before marking a run terminal, so this
    // final pull sees every batch - including for a run opened after it ended.
    pullItems();
  }, [settled, runId]);

  if (run.loading && !run.data) return <GhostLoader label="Loading run…" />;
  if (run.error) return <ErrorBanner error={run.error} />;
  const r = run.data!;
  const rows = toRows(output);
  const shareLabel = shareTerms(output?.platform ?? "").label;
  const batches = toBatches(batchItems);
  const batchPosts = batches.reduce((n, b) => n + b.posts.length, 0);
  const dataCount = batches.length ? batchPosts : rows.length;
  const dashboard = artifacts.find((a) => a.name.endsWith(".html"));
  const analysis = output?.analysis;

  return (
    <div className="stack">
      <div className="row between">
        <div>
          <button className="link back" onClick={onBack}><IconArrowLeft size={14} /> All runs</button>
          <h1 className="row" style={{ gap: 10 }}>
            <ActorName name={r.actor} size={34} /> <Badge value={r.status} />
            {/* Rides ALONGSIDE the status rather than replacing it: the run
                really did fail, and "PARTIAL" is a fact about its output. */}
            {r.partial ? <Badge value="PARTIAL" /> : null}
          </h1>
          <p className="sub" style={{ margin: 0 }}><code>{r.run_id}</code></p>
        </div>
        {active ? (
          <button className="danger" onClick={() => api.cancelRun(runId).then(() => run.reload()).catch(() => {})}>
            Cancel run
          </button>
        ) : (
          <button className="danger" onClick={() => {
            if (window.confirm("Delete this run and everything it collected — posts, comments, "
                               + "logs and reports? This cannot be undone.")) {
              api.deleteRun(runId).then(onBack).catch(() => run.reload());
            }
          }}>
            Delete run
          </button>
        )}
      </div>

      <div className="card facts">
        <div><div className="muted">Started</div>{fmtTime(r.started_at)}</div>
        <div><div className="muted">Finished</div>{fmtTime(r.finished_at)}</div>
        <div><div className="muted">Duration</div>{fmtDuration(r.duration_seconds)}</div>
        <div><div className="muted">Posts</div>{fmtNumber(output?.post_count ?? rows.length)}</div>
        <div>
          <div className="muted">Limits</div>
          {r.timeout_s ? `${Math.round(r.timeout_s / 60)} min` : "no time limit"}
          {" · "}{r.memory_mb ? (r.memory_mb >= 1024 ? `${+(r.memory_mb / 1024).toFixed(1)} GB` : `${r.memory_mb} MB`) : "no memory limit"}
          {r.cpus ? ` · ${r.cpus} CPU` : ""}
        </div>
        {r.peak_memory_mb ? (
          <div><div className="muted">Peak memory</div>{fmtNumber(r.peak_memory_mb)} MB</div>
        ) : null}
      </div>

      {/* WHY IT FAILED, and what to do about it.
          Before this the console showed one free-text line, so telling a rate
          limit from a dead session from a bug on our side meant opening the
          logs. The category answers that, and the advice below it answers the
          question a person actually arrived with -- whether this is theirs to
          fix. Falls back to the old single line when a run carries no
          classification, which every run written before this does. */}
      {r.error_info ? (
        <Banner kind="bad">
          <div>
            <strong>{r.error_info.code}</strong>
            {r.error_info.detail ? <span className="muted"> · {r.error_info.detail}</span> : null}
          </div>
          <div style={{ marginTop: 4 }}>{r.error_info.message || r.error}</div>
          <div className="muted" style={{ marginTop: 6 }}>{failureAdvice(r.error_info)}</div>
          <div className="muted" style={{ marginTop: 4, fontSize: "0.9em" }}>
            {failureFacts(r.error_info, r.attempts)}
          </div>
        </Banner>
      ) : r.error ? (
        <Banner kind="bad"><strong>{r.status}</strong> — {r.error}</Banner>
      ) : null}

      {/* A FAILED RUN THAT STILL COLLECTED SOMETHING. Said separately from the
          error, and in a different key, because it changes what the tabs below
          are worth: the usual reading of "failed" is that there is nothing to
          look at, and here there is. */}
      {r.partial ? (
        <Banner kind="warn">
          This run did not finish, but it collected data before it stopped — what
          is below is partial rather than empty.
        </Banner>
      ) : null}
      {analysis?.error ? (
        <Banner kind="warn">
          The scrape succeeded but the analysis did not: {analysis.error}. The
          data below is complete.
        </Banner>
      ) : null}

      <div className="tabs">
        {(["logs", "data", "report"] as const).map((t) => (
          <div key={t} className={`tab ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>
            {t === "logs" ? (active ? <>Live logs <span className="spin" /></> : "Logs")
              : t === "data" ? `Data${dataCount ? ` (${dataCount})` : ""}` : "Report"}
          </div>
        ))}
      </div>

      {tab === "logs" ? (
        <pre className="logs" ref={(el) => { if (el && active) el.scrollTop = el.scrollHeight; }}>
          {logs || (active ? "Waiting for the first output…" : "No logs.")}
        </pre>
      ) : null}

      {tab === "data" ? (
        batches.length
          // A run delivered in batches is readable batch by batch WHILE it runs.
          ? <Batches batches={batches} platform={output?.platform ?? ""} shareLabel={shareLabel}
                     active={active} pageName={output?.page_information?.page_name ?? ""} />
          : active ? <p className="muted">The data appears when the run finishes.</p>
          // Sections only for a group or a run over BATCH_SIZE posts; anything
          // smaller is one plain table, as it always was.
          : output?.page_information?.page_type === "group" || rows.length > BATCH_SIZE
            ? <Batches batches={chunkPosts(output?.posts ?? [])} platform={output?.platform ?? ""}
                       shareLabel={shareLabel} active={false}
                       pageName={output?.page_information?.page_name ?? ""} />
            : <Dataset rows={rows} shareLabel={shareLabel}
                       sharesKey={shareTerms(output?.platform ?? "").key}
                       pageName={output?.page_information?.page_name ?? ""} />
      ) : null}

      {tab === "report" ? (
        <div className="stack">
          {!artifacts.length ? (
            <EmptyState compact title={active ? "Report in progress" : "No report"}>
              {active ? "The report is produced when the run finishes."
                      : "This run produced no report."}
            </EmptyState>
          ) : (
            <>
              <div className="artifacts">
                {artifacts.map((a) => (
                  <a key={a.name} className="artifact" href={api.artifactUrl(runId, a.name)}
                     target="_blank" rel="noreferrer">
                    <span className="file-icon"><IconFile size={22} /></span>
                    <span style={{ minWidth: 0 }}>
                      <span className="name" style={{ display: "block" }}>{a.name}</span>
                      <span className="meta">{fmtBytes(a.bytes)}</span>
                    </span>
                  </a>
                ))}
              </div>
              {analysis?.page ? (
                <div className="card stack">
                  <div><strong>Rating:</strong> {analysis.page.performance_rating}</div>
                  <p style={{ margin: 0 }}>{analysis.page.executive_summary}</p>
                </div>
              ) : null}
              {dashboard ? (
                <iframe className="frame" style={{ height: 760, background: "#fff" }}
                        src={api.artifactUrl(runId, dashboard.name)} title="dashboard" />
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}
