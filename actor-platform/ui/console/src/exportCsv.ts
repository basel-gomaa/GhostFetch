/** Pure export builders for the Data tab. No DOM, no React, so they can be
 *  checked outside a browser.
 *
 *  Both formats give comments a SECTION of their own rather than leaving them
 *  mixed in among a post's fields under the scraper's internal names:
 *    CSV  -- each post's row, with its comments and replies listed directly
 *            under it (indented type label), so it reads like the page.
 *    JSON -- per post a "post" block, a "comments_summary" block and a
 *            "comments" list with replies nested under their comment, using
 *            plain field names. Internal bookkeeping (feedback_id, engagement
 *            score) is left out. */

/** Quote everything and double any embedded quote. A caption WILL contain a
 *  comma and a newline, and a spreadsheet reading one post as five rows is the
 *  classic way an export quietly lies. */
export const cell = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;

/** Excel reads a BOM-less UTF-8 file as the local ANSI code page, which turns
 *  Arabic (and any non-Latin) captions and comments into mojibake. */
export const CSV_BOM = "﻿";

const list = (v: any): any[] => (Array.isArray(v) ? v : []);

/** A count as a number. The scraper emits some counts as strings ("1"). */
const num = (v: any): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(String(v).replace(/,/g, ""));
  return Number.isFinite(n) ? n : null;
};

/** "2026-08-21T01:10:38+00:00" -> "2026-08-21 01:10" (UTC) for a spreadsheet. */
const shortDate = (v: any): string => {
  if (!v) return "";
  const t = Date.parse(String(v));
  return Number.isNaN(t) ? String(v) : new Date(t).toISOString().slice(0, 16).replace("T", " ");
};

/** How many top-level comments and replies a list of posts carries. */
export function countThreads(posts: any[]): { comments: number; replies: number } {
  let comments = 0, replies = 0;
  for (const p of posts) {
    for (const c of list(p?.comments)) {
      comments += 1;
      replies += list(c?.reply_comments).length;
    }
  }
  return { comments, replies };
}

// --- CSV --------------------------------------------------------------------

/** POST row, then its COMMENT rows, each followed by its REPLY rows. The type
 *  label is indented so the nesting is visible in a spreadsheet; `reply_to`
 *  names the comment's author, so a reply is readable without the ids. */
export function toThreadCsv(posts: any[], shareLabel: string, sharesKey = "shares",
                            pageName = ""): string {
  const head = ["type", "date (UTC)", "author", "text", "likes", "comments",
                shareLabel.toLowerCase(), "url", "author_url", "reply_to", "post_type", "id"];
  const lines = [head.join(",")];
  const row = (values: unknown[]) => lines.push(values.map(cell).join(","));
  for (const p of posts) {
    row(["POST", shortDate(p.post_date ?? p.created_at), p.author ?? pageName,
         p.caption ?? p.text ?? "", num(p.likes), num(p.comments_count) ?? list(p.comments).length,
         num(p[sharesKey]), p.post_url ?? "", "", "", p.content_type ?? "", p.post_id ?? ""]);
    for (const c of list(p.comments)) {
      const replies = list(c.reply_comments);
      row([" COMMENT", shortDate(c.comment_date), c.username ?? "", c.comment ?? "",
           num(c.likes), replies.length || num(c.replies) || 0, "", c.comment_url ?? "",
           c.profile_url ?? "", "", "", c.comment_id ?? ""]);
      for (const r of replies) {
        row(["  REPLY", shortDate(r.comment_date), r.username ?? "", r.comment ?? "",
             num(r.likes), "", "", r.comment_url ?? "", r.profile_url ?? "",
             c.username ?? "", "", r.comment_id ?? ""]);
      }
    }
  }
  return lines.join("\n");
}

// --- JSON -------------------------------------------------------------------

/** Drop keys whose value is null/undefined/"", so a block says only what is known. */
const compact = (o: Record<string, any>) =>
  Object.fromEntries(Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== ""));

function cleanComment(c: any, withReplies: boolean): Record<string, any> {
  const out = compact({
    id: c.comment_id, author: c.username, author_url: c.profile_url,
    date: c.comment_date, text: c.comment, likes: num(c.likes), url: c.comment_url,
  });
  if (withReplies) out.replies = list(c.reply_comments).map((r) => cleanComment(r, false));
  return out;
}

function cleanItem(p: any, sharesKey: string): Record<string, any> {
  const comments = list(p.comments);
  const cc = p.comment_collection ?? {};
  const post = compact({
    url: p.post_url, date: p.post_date ?? p.created_at, caption: p.caption ?? p.text,
    type: p.content_type, likes: num(p.likes), comments_reported: num(p.comments_count),
    [sharesKey]: num(p[sharesKey]), views: num(p.views),
  });
  if (list(p.media).length) post.media = p.media;
  const summary = compact({
    collected: comments.length,
    replies: comments.reduce((n, c) => n + list(c.reply_comments).length, 0),
    requested: cc.requested_comments,
    sort: cc.comment_sort,
    stopped_because: cc.stop_reason,
    complete: cc.collection_complete,
    window: cc.comment_window,
    written_in_window: cc.comments_in_window,
    written_outside_window: cc.comments_out_of_window,
  });
  return { post, comments_summary: summary, comments: comments.map((c) => cleanComment(c, true)) };
}

/** The whole export as one document: what it is, its totals, then the items. */
export function toThreadJson(name: string, posts: any[], sharesKey = "shares"): string {
  const { comments, replies } = countThreads(posts);
  return JSON.stringify({
    batch: name, exported_at: new Date().toISOString(),
    posts: posts.length, comments, replies,
    items: posts.map((p) => cleanItem(p, sharesKey)),
  }, null, 2);
}
