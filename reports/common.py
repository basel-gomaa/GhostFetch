"""Platform vocabulary shared by the report and dashboard renderers.

Everything here answers the same kind of question: **what does this metric mean
on this platform, and is it even available?** The two renderers
(the text/PDF renderer and `dashboard.py` for HTML) must answer those
questions identically or the same run produces two different stories, so the
answers live here once rather than being copied into both.

Deliberately dependency-free - no reportlab, no HTML escaping, no I/O. That
matters: `dashboard.py` generates HTML and must not pull reportlab in
transitively just to ask whether a platform publishes view counts.

NOT moved here, and why: `_bullets()` is also duplicated, between the
text/PDF renderer and `pdf_report.py`. It is a reportlab flowable helper, and
those two files belong to the two report systems this repo keeps deliberately
separate (the Facebook-shaped one and YouTube's simpler one).
Unifying a ten-line helper would couple them and force reportlab into this
module. It stays duplicated on purpose.
"""

__all__ = ["share_terms", "publishes_views", "audience_label",
           "target_label", "coverage_note", "cap_note"]


def share_terms(platform: str):
    """(post_key, stats_total_key, stats_avg_key, display_label) for "shares".

    Instagram has no "shares" - its public share-equivalent is REPOSTS, carried
    under its own keys by the Instagram processor. Every other platform
    (including Threads, whose reposts are a genuine share metric) uses the
    default wording.
    """
    if platform == "Instagram":
        return ("reposts", "total_reposts", "average_reposts", "Reposts")
    return ("shares", "total_shares", "average_shares", "Shares")


def target_label(page_data: dict) -> str:
    """"Group" or "Page" - what this report's subject IS.

    Same reasoning as `audience_label`: a report headed "Page: <group name>"
    misdescribes its own subject. Defaults to "Page" when `page_type` is absent.
    """
    return "Group" if (page_data or {}).get("page_type") == "group" else "Page"


def audience_label(page_data: dict) -> str:
    """What the audience number under this target is CALLED.

    A Facebook GROUP states its size as "12K members" and has no followers at
    all; a page has followers and no members. Printing "Followers: 0" over a
    group (which is what the followers-only DOM regex produced) reads as a
    scrape that failed rather than the wrong noun for a number that exists.

    Keyed on the processed doc's `page_type`, which is absent on every raw doc
    written before groups were supported - so this returns "Followers" for them
    and their reports stay byte-identical.
    """
    return "Members" if (page_data or {}).get("page_type") == "group" else "Followers"


def publishes_views(platform: str) -> bool:
    """Does this platform expose a public per-post view/impression count?

    Threads does NOT (verified against its payload - the only view-shaped keys
    are `"view_counts": false` and a card-attachment struct), so its collector
    correctly emits `views: null` on every post. Rendering "Views: 0 total" for
    it reads as a scrape that failed rather than a metric the platform never
    published, which is the opposite of true - so the report says so in words
    instead of printing a zero.
    """
    return platform != "Threads"


def coverage_note(page_data: dict, comments_data: dict | None) -> str | None:
    """"N% of the M replies the platform reports", when the two differ a lot.

    Several platforms serve only a subset of a post's comments to a logged-out
    or public caller - Threads renders roughly 5-10% of them and LinkedIn's
    public post page a variable handful - so a report that shows "Total
    comments 19,928" beside "Comments analyzed 393" looks like a broken scrape
    unless it says which of the two numbers is the platform's and which is
    ours. Returns None when the gap is small enough not to need explaining.
    """
    reported = (page_data.get("statistics") or {}).get("total_comments") or 0
    analysed = sum(len(p.get("comments") or [])
                   for p in (comments_data or {}).get("posts") or [])
    if not reported or not analysed or analysed >= reported * 0.9:
        return None
    share = analysed / reported
    # A sub-1% ratio rounds to "0%", which reads as "we got nothing" when we in
    # fact got the figure printed right next to it.
    pct = "<1%" if share < 0.01 else f"{share:.0%}"
    return f"{pct} of the {reported:,} the platform reports"


def cap_note(post: dict) -> str | None:
    """Why THIS post's comment list stopped where it did - or None.

    A bare "(16 comment(s))" next to nineteen "(20 comment(s))" reads as a
    partial failure. Usually it is the opposite: the collector took everything
    the platform served publicly and recorded `stop_reason: public_cap`, while
    the posts showing 20 simply hit the caller's own --comments limit. Only the
    platforms that keep this accounting (Threads, LinkedIn, X) have the field;
    for the others it is absent and this returns None, leaving their reports
    byte-identical.

    Returns None for `requested_met` too - that is the expected case and needs
    no explanation.
    """
    reason = post.get("stop_reason")
    if not reason or reason == "requested_met":
        return None
    if reason == "window_exhausted":
        # A DATE-WINDOWED run. `comments_count` is the post's ALL-TIME total and
        # is never the denominator for "the comments written in July", so
        # printing the two side by side reads as a 60% shortfall on a complete
        # collection. Name the window and the comments it deliberately excluded.
        outside = post.get("comments_out_of_window")
        window = post.get("comment_window")
        if outside and window:
            return f"all within {window}; {outside:,} outside it not collected"
        if window:
            return f"all within {window}"
        return "all within the requested date range"
    if reason == "public_cap":
        available = post.get("available_comments")
        # The honest denominator is what the public page served, NOT the count
        # the platform reports - the gap between them is the login wall.
        if available:
            return f"all {available} the platform served publicly"
        return "all the platform served publicly"
    if reason == "source_exhausted":
        return "all replies on the post"
    if reason in ("none_returned", "post_unavailable"):
        return "platform served none"
    if reason in ("rate_limited", "fetch_failed"):
        return "stopped early - platform throttled or failed"
    if reason in ("replay_stalled", "dom_stalled"):
        # Facebook: the cursor loop (or the comment-list loader) stopped serving
        # before --comments was met.
        return "platform stopped serving more"
    if reason == "no_comments_on_post":
        return None          # zero comments needs no footnote
    return reason.replace("_", " ")


def segmentation_note(segmentation) -> str:
    """One line saying what the segmentation counts are counts OF.

    Three separate facts, and a reader who is told only the first can read the
    table as the whole corpus: how much was labelled, how much the model
    skipped, and how much was never sent because the run exceeded
    `ai.segmentation.MAX_ITEMS_TO_LABEL`. The last one is the difference
    between "these are the topics in this page's conversation" and "these are
    the topics in the busiest 1,500 items of it".
    """
    note = (f"{segmentation.post_count} post(s) and "
            f"{segmentation.comment_count} comment(s) labelled")
    if getattr(segmentation, "unlabelled", 0):
        note += f"; {segmentation.unlabelled} unlabelled"
    not_sampled = getattr(segmentation, "not_sampled", 0)
    if not_sampled:
        total = getattr(segmentation, "total_items", 0)
        note += (f" - a sample of {total - not_sampled:,} of {total:,} items "
                 "(highest-liked threads, spread across posts)")
    return note
