"""Shared post-date window: --start-date / --end-date for every platform.

Dependency-free and Playwright-free on purpose, like each collector package's
``common.py``, so any collector can import it without creating a cycle.

The semantics that matter, and the reason this is not just a list filter:

    A window is something the collector must **cover**, not merely filter to.

``--posts 50 --start-date 2025-08-01 --end-date 2025-10-01`` means "the posts in
that window, up to 50" - NOT "take the newest 50, then drop those outside the
window". The second reading silently returns nothing whenever the account has
posted 50 times since October, which is the common case for an active account.
So ``--posts`` caps the WINDOW results, and paging continues past the newest N
until the window has been cleared or the page budget runs out.

Two mechanisms, picked per feed by whether its fetch order is reverse-chronological:

  * **Early stop** - a chronological feed (profile timelines on Threads, X,
    Instagram, TikTok, LinkedIn) can stop as soon as it has paged *past*
    start_date, because nothing older will appear again.
  * **Scan and filter** - a ranked feed (hashtag/search grids, and Facebook,
    whose ordering nothing in the codebase actually asserts) must page normally
    and filter at the end, because an old post can follow a new one.

Either way a run that stops early against the budget must SAY SO rather than
present a partial window as complete - see ``WindowResult``.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

__all__ = ["DateWindow", "WindowResult", "WindowTracker", "parse_window",
           "resolve_count", "filter_posts", "OLDER_PAGE_LIMIT", "TRACE_ENV",
           "POST_ORDER", "PAGE_ORDER", "ENVELOPE_PAGES", "split_window",
           "split_evenly"]

logger = logging.getLogger(__name__)

# Per-page window instrumentation, OFF unless this env var names a file. It is
# opt-in because it must be provably free: with it unset nothing is computed,
# nothing is logged and `as_dict()` is untouched, so every existing run's output
# stays byte-identical. Set it to a path and each page appends one JSON line.
#
# It records post TIMESTAMPS and COUNTS only. There is no way for a cookie or a
# token to reach it - the tracker is handed dates, never a session - so the "do
# not log secrets" rule is satisfied structurally rather than by redaction.
TRACE_ENV = "FB_WINDOW_TRACE"

# Consecutive pages entirely older than the window before paging stops. One is
# tolerated so a single out-of-order item cannot truncate an otherwise good run.
OLDER_PAGE_LIMIT = 2

# WHICH SIGNAL DISARMS THE EARLY STOP. See WindowTracker.observe_page_order.
#
#   POST_ORDER - the original: any pairwise inversion between two consecutive
#                posts in fetch order. The default, and what every non-group
#                caller still uses.
#   PAGE_ORDER - a page whose newest post is newer than the newest of the last
#                ENVELOPE_PAGES pages. Opt-in, currently only the Facebook GROUP
#                feed, which is the one feed that can be ASKED for chronological
#                order (sorting_setting=CHRONOLOGICAL).
#
# Measured on a large public group, 763 traced pages across a 1-day and a 7-day
# window (FB_WINDOW_TRACE): POST_ORDER fired 44 and 39 times, first at page 14
# and page 13 of every run, which disarms the early stop almost immediately and
# then scans the whole page budget. PAGE_ORDER fired ZERO times on the same
# traces. Replaying page_done's unchanged rule over those traces as if it had
# never been disarmed missed ZERO in-window posts (163/163 and 622/622).
#
# The disorder is real; it is just entirely WITHIN pages. Every page carries
# exactly 3 posts and stragglers as old as ten weeks appear among them without
# ever becoming a page's newest - so the page-level envelope descends where the
# post-level sequence does not.
ENVELOPE_PAGES = 3
POST_ORDER = "post"
PAGE_ORDER = "page"

# Fail-fast for an UNREACHABLE date window (Facebook groups, chronological feed).
# After this many pages there is enough paging-velocity signal to estimate
# whether date_from can be reached within the page budget; the margin is how far
# past the budget the estimate must land before we abort, so a window that is
# realistically reachable (needs <= budget) is never false-aborted.
FAILFAST_CALIBRATION_PAGES = 40
FAILFAST_MARGIN = 1.25

DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# Optional time-of-day forms, tried in order before falling back to date-only.
# The brief's from_datetime/to_datetime use HH:MM:SS; HH:MM is accepted too.
_DATETIME_FORMATS = (DATETIME_FORMAT, "%Y-%m-%d %H:%M")


def _parse_day(value, end_of_day=False):
    """Parse a bound as ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM[:SS]`` (UTC).

    The ISO ``T`` separator is accepted. ``end_of_day`` rounds to the last
    microsecond of the day, but ONLY when no explicit time was given: a date-only
    ``--end-date`` still spans the whole day (so a bare day range is inclusive and
    the brief's ``00:00:00``/``23:59:59`` example is reproduced exactly), while
    ``--end-date "2026-07-07 17:00"`` means exactly 17:00.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        # An explicit datetime is taken as given - never rounded to end-of-day.
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("T", " ", 1)
    moment, had_time = None, False
    for fmt in _DATETIME_FORMATS:
        try:
            moment = datetime.strptime(text, fmt)
            had_time = True
            break
        except ValueError:
            continue
    if moment is None:
        try:
            moment = datetime.strptime(text[:10], DATE_FORMAT)
        except ValueError as exc:
            raise ValueError(
                f"Invalid date/time {value!r} - expected YYYY-MM-DD or "
                "'YYYY-MM-DD HH:MM[:SS]' (e.g. 2025-08-01 or "
                "'2025-08-01 14:30:00')."
            ) from exc
    if end_of_day and not had_time:
        moment = moment.replace(hour=23, minute=59, second=59, microsecond=999999)
    return moment.replace(tzinfo=timezone.utc)


def _describe_moment(moment, is_end=False):
    """Render a bound: ``YYYY-MM-DD`` at a day boundary, else with the time.

    A start at midnight and an end at end-of-day are the date-only defaults, so
    they render as a bare date - keeping every existing date-only window's
    description (and the ``date_window`` document) byte-identical. A sub-day time
    is shown as ``YYYY-MM-DD HH:MM:SS`` so the precision is never hidden.
    """
    if moment is None:
        return None
    day_boundary = ((moment.hour == 23 and moment.minute == 59
                     and moment.second == 59) if is_end
                    else (moment.hour == 0 and moment.minute == 0
                          and moment.second == 0 and moment.microsecond == 0))
    return moment.strftime(DATE_FORMAT if day_boundary else DATETIME_FORMAT)


def _to_datetime(value):
    """Best-effort ISO-8601 / epoch -> aware datetime. None when unparseable."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Heuristic shared with the collectors: epoch milliseconds are ~1e12.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.strptime(text[:10], DATE_FORMAT)
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class DateWindow:
    """An inclusive [start, end] window over post dates, in UTC."""

    def __init__(self, start=None, end=None):
        self.start = start
        self.end = end

    def __bool__(self):
        return self.start is not None or self.end is not None

    def __repr__(self):
        return f"DateWindow(start={self.describe_start()}, end={self.describe_end()})"

    def describe_start(self):
        return _describe_moment(self.start, is_end=False)

    def describe_end(self):
        return _describe_moment(self.end, is_end=True)

    def contains(self, value, strict=False):
        """Is this post date inside the window?

        By default an UNDATED value is KEPT, never silently dropped: some tiers
        (LinkedIn's DOM fallback, for one) legitimately produce rows without a
        timestamp, and discarding them would lose real data to a filter they
        cannot answer. That is the right call for POSTS.

        ``strict=True`` flips it: an unknown/unparseable date is NOT treated as
        valid and is dropped. Used for COMMENT date filtering, where a comment
        whose timestamp cannot be read cannot be verified to fall in the range,
        and admitting it would silently pad the window with unverifiable rows.
        """
        moment = _to_datetime(value)
        if moment is None:
            return not strict
        if self.start and moment < self.start:
            return False
        if self.end and moment > self.end:
            return False
        return True

    def is_older_than_start(self, value):
        """True only for a post definitively older than the window.

        This is the early-stop predicate, so it is deliberately conservative:
        an undated or unparseable value returns False and keeps paging, because
        stopping on a value we could not read would truncate the run.
        """
        if not self.start:
            return False
        moment = _to_datetime(value)
        return moment is not None and moment < self.start

    def describe(self):
        if self.start and self.end:
            return f"{self.describe_start()} to {self.describe_end()}"
        if self.start:
            return f"since {self.describe_start()}"
        if self.end:
            return f"up to {self.describe_end()}"
        return "no date window"


def parse_window(start=None, end=None):
    """Build a DateWindow, or None when neither bound was supplied.

    Raises ValueError on an unparseable date or an inverted range, so the CLI
    fails immediately with a clear message instead of silently scraping nothing.
    """
    start_dt = _parse_day(start)
    end_dt = _parse_day(end, end_of_day=True)
    if start_dt and end_dt and start_dt > end_dt:
        raise ValueError(
            f"--start-date ({_describe_moment(start_dt)}) is after --end-date "
            f"({_describe_moment(end_dt, is_end=True)}) - the range is inverted.")
    if not start_dt and not end_dt:
        return None
    return DateWindow(start_dt, end_dt)


def resolve_count(num_posts, window, default, limit):
    """How many posts a run is asking for, or None for "no count at all".

    ``num_posts=None`` means NO count was requested. With a ``window`` that is
    the intended way to run: the date boundary is the stopping condition, and
    substituting a number here is NOT a harmless ceiling - by the time it
    reaches the paging loop it is indistinguishable from the caller having
    asked for exactly that many, so the loop measures itself against a
    threshold nobody set and stops early on a window it has not covered.
    Without a window there is nothing else to stop on, so None falls back to
    the collector's historical default.

    This is the single place that decision is made, which is what lets every
    collector rely on "num_posts is None implies a window". It is stated once
    here rather than six times because it has been got wrong in both possible
    directions: linkedin/instagram/tiktok raised TypeError on ``int(None)``,
    while x/threads wrote ``int(num_posts or NUM_POSTS)`` and silently turned
    "no count" into 50 - so a window holding 300 posts returned 50 and reported
    success. ``collectors/facebook`` had the only correct implementation, and
    this is it, generalised.
    """
    if num_posts is None:
        return None if window else default
    return max(1, min(int(num_posts), limit))


def split_evenly(window, parts):
    """Tile ``window`` into ``parts`` sub-windows of EQUAL duration.

    Equal duration, not fixed-size blocks: a fixed 5-day block over a 31-day
    window leaves 6 full chunks and a 1-day stub, and the wall clock of a
    parallel run is its SLOWEST chunk, so the stub buys nothing while the 5-day
    chunks set the pace. Dividing the span evenly puts the same work in every
    worker.

    Tiles exactly - each chunk starts one microsecond after the previous ends -
    so the union is the original window with no gap. Gaps would silently lose
    posts; the collector's own +-1 day seek margin supplies overlap at the seams,
    and dedup makes that free.
    """
    if not window or not window.start or not window.end or parts < 2:
        return [window] if window else []
    total = (window.end - window.start).total_seconds()
    if total <= 0:
        return [window]
    tick = timedelta(microseconds=1)
    step = total / parts
    chunks, cursor = [], window.start
    for i in range(parts):
        end = (window.end if i == parts - 1
               else window.start + timedelta(seconds=step * (i + 1)))
        if end < cursor:
            break
        chunks.append(DateWindow(cursor, end))
        cursor = end + tick
    return chunks or [window]


def split_window(window, days):
    """Tile ``window`` into consecutive sub-windows of ``days`` each.

    For PARALLEL date-chunk scraping. The chunks TILE exactly - each starts one
    microsecond after the previous ends - so the union is the original window
    with no gap and no overlap. Gaps would silently lose posts; overlap would be
    harmless (every caller dedupes by post id) but is not needed here, because
    the collector's own server-side seek already widens each request outward by
    a day and the client-side filter trims it back.

    Returns ``[window]`` unchanged unless BOTH bounds are known - an open-ended
    window has no length to divide - so callers can apply it unconditionally.
    """
    if not window or not window.start or not window.end or days < 1:
        return [window] if window else []
    chunks, cursor, step = [], window.start, timedelta(days=days)
    tick = timedelta(microseconds=1)
    while cursor <= window.end:
        end = min(cursor + step - tick, window.end)
        chunks.append(DateWindow(cursor, end))
        cursor = end + tick
    return chunks or [window]


def filter_posts(posts, window, date_key="post_date"):
    """Keep only in-window posts. A falsy window is a no-op."""
    if not window:
        return list(posts or [])
    return [p for p in posts or []
            if isinstance(p, dict) and window.contains(p.get(date_key))]


class WindowResult:
    """How a windowed run actually ended - recorded in the posts document.

    `complete` is the honest bit: False means the budget ran out before the
    window was cleared, so the caller must not present the result as the full
    window. Stop reasons: window_cleared | source_exhausted | page_budget |
    requested_met.
    """

    def __init__(self, window=None):
        self.window = window
        self.scanned_pages = 0
        self.oldest_seen = None
        self.newest_seen = None
        self.stop_reason = None
        # Fail-fast diagnostics: what the velocity estimate concluded, if it ran.
        self.estimated_pages_needed = None
        self.page_budget = None
        # One entry per DISTINCT post, for the robust reach below. Kept separate
        # from oldest_seen because note() is called from filter()/keep() and so
        # runs many times per post: its min is still correct, but its
        # DISTRIBUTION is not, and the reach needs a distribution.
        self._reach_dates = []
        # --- instrumentation only (see TRACE_ENV). Never read by any decision.
        self.page_trace = []
        self.disorder_events = []
        # The descending ENVELOPE of the feed: the oldest per-page baseline
        # (a page's newest post) reached so far. Backslide is how far a later
        # page's baseline rose back above it - the disorder amplitude any
        # early-stop margin would have to exceed. See the plan's section D.
        self.baseline_min = None
        self.max_backslide_s = 0.0

    def note(self, post_date):
        moment = _to_datetime(post_date)
        if moment is None:
            return
        if self.oldest_seen is None or moment < self.oldest_seen:
            self.oldest_seen = moment
        if self.newest_seen is None or moment > self.newest_seen:
            self.newest_seen = moment

    def note_reach(self, post_date):
        """Record one post's date ONCE - callers that can guarantee that.

        The feed's window_stop already dedupes by post id, so it is the one
        place that can feed a real distribution rather than a repeated sample.
        """
        moment = _to_datetime(post_date)
        if moment is not None:
            self._reach_dates.append(moment)

    @property
    def reach_oldest(self):
        """How far back paging ACTUALLY got, ignoring interleaved stragglers.

        `oldest_seen` is a true minimum, and on a group feed that makes it a
        liar: a handful of much older posts get bumped, pinned or re-shared into
        an otherwise recent feed, so a run whose entire bulk stayed inside
        August reported reaching 2025-08-11. Measured on a real run - that
        single outlier sat among 1,800 posts that never left one month.

        The 5th percentile answers what the reader is actually asking. Below 20
        samples there is no distribution to speak of, so fall back to the min.
        """
        if not self._reach_dates:
            return self.oldest_seen
        ordered = sorted(self._reach_dates)
        if len(ordered) < 20:
            return ordered[0]
        return ordered[int(len(ordered) * 0.05)]

    @property
    def window_reached(self):
        """Did paging get back INTO the window at all?

        False when the oldest the feed genuinely reached is still NEWER than the
        window's end: the feed never arrived, so whatever posts came through are
        stragglers interleaved from further back, not a sample of the window.
        This is what distinguishes "the window is partly covered" from "the
        window is further back than this feed can be paged", which a post COUNT
        cannot do - 4 posts and 1,795 posts both look like partial coverage.
        """
        if not self.window or not self.window.end:
            return True
        reach = self.reach_oldest
        return reach is None or reach <= self.window.end

    @property
    def complete(self):
        return self.stop_reason in ("window_cleared", "source_exhausted",
                                    "requested_met")

    def as_dict(self):
        if not self.window:
            return None
        return {
            "start": self.window.describe_start(),
            "end": self.window.describe_end(),
            "requested": self.window.describe(),
            "scanned_pages": self.scanned_pages,
            "oldest_seen": self.oldest_seen.isoformat() if self.oldest_seen else None,
            "newest_seen": self.newest_seen.isoformat() if self.newest_seen else None,
            # The robust reach, and whether paging ever arrived at the window.
            "reach_oldest": (self.reach_oldest.isoformat()
                             if self.reach_oldest else None),
            "window_reached": self.window_reached,
            "estimated_pages_needed": self.estimated_pages_needed,
            "page_budget": self.page_budget,
            "complete": self.complete,
            "stop_reason": self.stop_reason,
        }

    def reach_line(self):
        """What the feed ACTUALLY served, beside what was asked for.

        A windowed feed run logs `1/1000 target`, which cannot distinguish "the
        feed has not paged back far enough yet" from "the feed is not in the
        order we asked for". Naming the span reached answers both at a glance.
        """
        if not self.window:
            return None
        fmt = lambda m: m.strftime(DATE_FORMAT) if m else "unknown"  # noqa: E731
        line = (f"window {self.window.describe()}; feed served "
                f"{fmt(self.newest_seen)} back to {fmt(self.reach_oldest)} "
                f"over {self.scanned_pages} page(s)")
        # Name the straggler separately rather than letting it BE the reach.
        if (self.oldest_seen and self.reach_oldest
                and self.oldest_seen < self.reach_oldest):
            line += (f" (plus stragglers back to {fmt(self.oldest_seen)}, "
                     "interleaved rather than paged to)")
        if not self.window_reached:
            line += " - paging never reached the window"
        if self.estimated_pages_needed and self.page_budget:
            line += (f"; est. ~{self.estimated_pages_needed} page(s) needed to "
                     f"reach {self.window.describe_start()} vs budget "
                     f"{self.page_budget} - stopped early")
        return line

    def note_line(self):
        """One honest line for the log."""
        if not self.window:
            return None
        if self.complete:
            return (f"date window {self.window.describe()} covered "
                    f"({self.scanned_pages} page(s) scanned)")
        oldest = self.oldest_seen.strftime(DATE_FORMAT) if self.oldest_seen else "unknown"
        return (f"window NOT fully covered - scanned {self.scanned_pages} page(s), "
                f"reached back to {oldest} (requested start "
                f"{self.window.describe_start()})")


class WindowTracker:
    """Per-run bookkeeping for one windowed paging loop.

    The six collectors page in wildly different ways - GraphQL cursors, REST
    max_id, signed URLs, offset windows, a YouTube playlist - but they all need
    the SAME four decisions, and getting any of them subtly wrong is silent:

      1. is this post in the window?                     -> keep()
      2. how far back have we actually reached?          -> note()/oldest_seen
      3. may we stop paging yet?                         -> page_done()
      4. was the window actually covered, or did we run
         out of budget?                                  -> finish()/as_dict()

    Putting them here means the semantics are defined once instead of being
    re-derived (and re-broken) per platform.

    A tracker with no window is falsy and every method is a no-op that keeps
    everything, so callers can pass one unconditionally rather than branching.
    """

    def __init__(self, window=None, older_page_limit=OLDER_PAGE_LIMIT,
                 chronological=True, order_signal=POST_ORDER):
        self.window = window
        self.result = WindowResult(window)
        # Early stop is only sound on a feed whose fetch order is known to be
        # newest-first. On a ranked feed (hashtag/search grids) an old post can
        # follow a new one, so we must scan the whole budget and filter.
        self.chronological = chronological
        self.older_page_limit = older_page_limit
        self._older_pages = 0
        # Latches True once paging has demonstrably gone past the window, so a
        # caller that cannot see the loop's return value can still tell
        # "covered the window" from "ran out of budget".
        self.cleared = False
        # observe_order() state - only used by callers that opt in.
        self._last_seen = None
        self.order_broken_at = None
        # The post BEFORE _last_seen. Instrumentation only: it is what lets an
        # inversion be classified (see _note_disorder) as the benign recovery
        # from a spliced-in older post versus a genuinely newer post arriving
        # late. No decision reads it.
        self._prev_seen = None
        self._trace_path = os.environ.get(TRACE_ENV) or None
        # Which signal is allowed to disarm the early stop (see POST_ORDER /
        # PAGE_ORDER). A scan-and-filter tracker never arms one in the first
        # place, so it is pinned to the default rather than left ambiguous.
        self.order_signal = order_signal if chronological else POST_ORDER
        # The last ENVELOPE_PAGES page baselines, for PAGE_ORDER.
        self._page_baselines = []
        # budget_unreachable() announces its estimate once, not every page.
        self._estimate_logged = False

    def __bool__(self):
        return bool(self.window)

    def keep(self, post_date):
        """Is this post in the window? Also records how far back we have got."""
        self.result.note(post_date)
        if not self.window:
            return True
        return self.window.contains(post_date)

    def filter(self, posts, date_key="post_date"):
        """Filter a shaped post list, recording every date it sees."""
        if not self.window:
            return list(posts or [])
        kept = []
        for post in posts or []:
            if not isinstance(post, dict):
                continue
            if self.keep(post.get(date_key)):
                kept.append(post)
        return kept

    def observe_order(self, post_date):
        """Watch the fetch order and DEMOTE to scan-and-filter if it breaks.

        For the one feed that claims to be newest-first but whose claim nothing
        in this codebase can prove. Facebook groups can be asked for
        CHRONOLOGICAL ordering (`common.group_feed_url`), which makes early stop
        sound - but Facebook's feed order must not
        be *assumed*, and an early stop on a feed that is not really ordered
        truncates the run silently.

        So the caller opts in with `chronological=True` and calls this for every
        post in fetch order. As long as the dates keep descending, early stop
        stays armed. The first genuine inversion latches it off for the rest of
        the run, degrading to the scan-and-filter behaviour that was always the
        Facebook default. One-way: it never re-arms, because a feed that has
        reordered once cannot be trusted for the rest of the run.

        Returns True while the order still holds. Undated posts are skipped
        rather than treated as an inversion - some tiers legitimately produce
        rows without a timestamp.
        """
        moment = _to_datetime(post_date)
        if moment is None:
            return self.chronological
        previous, prior = self._last_seen, self._prev_seen
        self._prev_seen = previous
        self._last_seen = moment
        inverted = previous is not None and moment > previous
        # INSTRUMENTATION: keep classifying inversions even after the latch has
        # dropped, so a run yields the whole disorder DISTRIBUTION rather than
        # the single event that happened to fire first. Recording is gated on
        # the trace path and touches no decision; the disarm below is unchanged.
        if inverted and self._trace_path:
            self._note_disorder(moment, previous, prior)
        if not self.chronological:
            return False
        # Under PAGE_ORDER the disarm decision belongs to observe_page_order,
        # which judges the feed's page-level ENVELOPE. A pairwise inversion
        # there is expected - it is what a straggler inside a page looks like -
        # so it is recorded above and then deliberately not acted on.
        if self.order_signal == PAGE_ORDER:
            return True
        if inverted:
            self.chronological = False
            self.order_broken_at = moment
            self._older_pages = 0   # a half-counted streak must not carry over
            return False
        return True

    def observe_page_order(self, page_dates):
        """PAGE-level order verification - the opt-in alternative to the
        post-level one, for a feed that can be ASKED for chronological order.

        Call once per page with the dates NEW on that page, BEFORE page_done, so
        a page that breaks the order cannot also advance the older-page streak.

        The signal is the page's BASELINE - its newest post - compared against
        the newest of the previous ``ENVELOPE_PAGES`` pages. A rise there means
        the feed genuinely went backwards; anything else is a straggler sitting
        inside a page, which moves the page's oldest but not its newest.

        WHY THE ROLLING MAX AND NOT JUST THE PREVIOUS PAGE. Measured: a single
        page whose three posts were ALL stragglers (newest 8-19, oldest 8-17,
        inside an 8-21 stream) dipped the envelope on its own, and the next page
        - back on the 8-21 backbone - then read as a rise against it. Comparing
        against the previous page alone fired on exactly that (1 false disarm in
        323 pages); comparing against the last 3 fired zero times on the same
        traces, because the dip cannot outvote the two good pages around it.

        THE STOP RULE IS NOT CHANGED BY ANY OF THIS. page_done still requires
        ``older_page_limit`` consecutive pages whose posts are ALL older than
        the window start, and that rule is what actually survives the straggler
        page: the page after the dip carries an in-window post and resets the
        streak. An envelope-triggered STOP would have fired on the dip and lost
        the posts still to come - which is why this method only ever DISARMS.

        Returns True while the order still holds. One-way, like the post-level
        signal: a feed that has genuinely reordered is not trusted again.
        """
        if self.order_signal != PAGE_ORDER or not self.chronological:
            return self.chronological
        moments = [m for m in (_to_datetime(d) for d in page_dates or [])
                   if m is not None]
        if not moments:
            return True
        baseline = max(moments)
        # Judge only once a full comparison window exists - matching the
        # measurement, and avoiding a verdict from one or two samples.
        if len(self._page_baselines) >= ENVELOPE_PAGES:
            if baseline > max(self._page_baselines):
                self.chronological = False
                self.order_broken_at = baseline
                self._older_pages = 0   # a half-counted streak must not carry over
                if self._trace_path:
                    self.result.disorder_events.append({
                        "type": "disorder",
                        "page": self.result.scanned_pages + 1,
                        "kind": "envelope_rise",
                        "at": baseline.isoformat(),
                        "previous": max(self._page_baselines).isoformat(),
                        "prior": None,
                        "rise_s": (baseline
                                   - max(self._page_baselines)).total_seconds(),
                        "splice_depth_s": None,
                        "already_disarmed": False,
                    })
                    self._trace_write(self.result.disorder_events[-1])
                logger.info("[window] page-envelope rise at page %d (%s newer "
                            "than the last %d page(s)) - early stop disarmed, "
                            "falling back to scan-and-filter",
                            self.result.scanned_pages + 1,
                            baseline.isoformat(), ENVELOPE_PAGES)
                self._page_baselines = []
                return False
        self._page_baselines.append(baseline)
        if len(self._page_baselines) > ENVELOPE_PAGES:
            del self._page_baselines[0]
        return True

    def rearm(self):
        """Re-arm order verification from scratch (a caller that discovers the
        feed it was watching was not the target after all)."""
        self.chronological = True
        self._last_seen = None
        self._prev_seen = None
        self._page_baselines = []

    def _note_disorder(self, moment, previous, prior):
        """Classify one inversion. Instrumentation only - never read by a decision.

        Two shapes, and only one of them can threaten an early stop:

          * ``splice_recovery`` - ``previous`` was an older post spliced into an
            otherwise descending stream, and ``moment`` merely returns to the
            level the stream was already at (``moment <= prior``). Benign: an
            old post arriving early can only be collected sooner. This is the
            shape measured on a real group feed.
          * ``regression`` - ``moment`` is newer than the stream level itself
            (``moment > prior``), i.e. a genuinely newer post arriving late.
            This is the ONLY shape that could hide an in-window post behind a
            boundary the run has already paged past.

        ``prior`` is the post before ``previous``, which is the cheapest proxy
        for "the stream level ignoring the outlier". With no prior (an inversion
        on the second post of the run) the shape is unknowable, not assumed.
        """
        if prior is None:
            kind = "unknown"
        elif moment > prior:
            kind = "regression"
        else:
            kind = "splice_recovery"
        event = {
            "type": "disorder",
            "page": self.result.scanned_pages + 1,
            "kind": kind,
            "at": moment.isoformat(),
            "previous": previous.isoformat(),
            "prior": prior.isoformat() if prior else None,
            # How far the stream jumped back toward "newer".
            "rise_s": (moment - previous).total_seconds(),
            # How far below the stream level the spliced post sat.
            "splice_depth_s": ((prior - previous).total_seconds()
                               if prior else None),
            "already_disarmed": not self.chronological,
        }
        self.result.disorder_events.append(event)
        self._trace_write(event)

    def _trace_write(self, record):
        """Append one JSON line to the trace file. Never fails a scrape."""
        try:
            with open(self._trace_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.debug("window trace write failed: %s", exc)

    def page_done(self, page_dates):
        """Call once per page with the dates of the posts NEW on that page.

        Returns True when paging should stop because the feed has moved past
        the window. Only ever True on a chronological feed - see __init__.

        `page_dates` must EXCLUDE anything that repeats across pages (X re-sends
        its pinned post on every page, and a pinned post can be years old, so
        counting it would end the run on page one).
        """
        self.result.scanned_pages += 1
        if not self.window or not self.chronological or not self.window.start:
            return False
        dates = [d for d in (page_dates or []) if d]
        if not dates:
            self._older_pages = 0
            return False
        if all(self.window.is_older_than_start(d) for d in dates):
            self._older_pages += 1
            if self._older_pages >= self.older_page_limit:
                self.cleared = True
            return self.cleared
        self._older_pages = 0
        return False

    @property
    def tracing(self):
        """Is per-page instrumentation on? (``FB_WINDOW_TRACE`` names a file.)"""
        return bool(self._trace_path)

    def trace_page(self, page_dates, unique_posts=None, elapsed=None,
                   cumulative=None, stopped=None):
        """Record one page of the windowed crawl. NO-OP unless tracing is on.

        Called once per replayed page / scroll round, straight after
        ``page_done``, with the same date list - so the trace describes exactly
        the evidence the stop decision saw. It computes the descending-envelope
        metrics (baseline, backslide) that section D of the plan needs in order
        to say whether a bounded early stop is safe on this feed.

        Deliberately writes nothing into ``as_dict()``: the posts document must
        stay byte-identical whether or not tracing ran.
        """
        if not self._trace_path:
            return None
        moments = sorted(m for m in (_to_datetime(d) for d in page_dates or [])
                         if m is not None)
        window = self.window
        newest = moments[-1] if moments else None
        # BASELINE = the page's newest post: the level the feed has descended to,
        # robust to an older post spliced in among them (which moves the min but
        # not the max). BACKSLIDE = how far that level rose back above the oldest
        # baseline reached so far, measured BEFORE this page updates the envelope.
        backslide = 0.0
        if newest is not None:
            if self.result.baseline_min is not None:
                backslide = max(0.0,
                                (newest - self.result.baseline_min).total_seconds())
            if (self.result.baseline_min is None
                    or newest < self.result.baseline_min):
                self.result.baseline_min = newest
        if backslide > self.result.max_backslide_s:
            self.result.max_backslide_s = backslide

        in_window = before = after = 0
        for moment in moments:
            if window and window.start and moment < window.start:
                before += 1
            elif window and window.end and moment > window.end:
                after += 1
            else:
                in_window += 1

        record = {
            "type": "page",
            "page_number": self.result.scanned_pages,
            "number_of_posts": len(moments),
            "newest_post_timestamp": newest.isoformat() if newest else None,
            "oldest_post_timestamp": moments[0].isoformat() if moments else None,
            "number_in_window": in_window,
            "number_before_window": before,
            "number_after_window": after,
            "unique_posts": unique_posts,
            "elapsed_time": round(elapsed, 3) if elapsed is not None else None,
            "cumulative_time": round(cumulative, 3) if cumulative is not None else None,
            # WindowTracker state at the moment the decision was taken.
            "early_stop": "enabled" if self.chronological else "disabled",
            "older_pages_streak": self._older_pages,
            "cleared": self.cleared,
            "baseline_min": (self.result.baseline_min.isoformat()
                             if self.result.baseline_min else None),
            "backslide_s": backslide,
            "max_backslide_s": self.result.max_backslide_s,
            "disorder_events": len(self.result.disorder_events),
            "stop_reason": stopped,
        }
        self.result.page_trace.append(record)
        self._trace_write(record)
        logger.info("[window] page %s: %s post(s), newest %s oldest %s, "
                    "in_window=%s before=%s after=%s, early_stop=%s, "
                    "backslide=%.0fs (max %.0fs)",
                    record["page_number"], record["number_of_posts"],
                    record["newest_post_timestamp"],
                    record["oldest_post_timestamp"], in_window, before, after,
                    record["early_stop"], backslide,
                    self.result.max_backslide_s)
        return record

    def trace_summary(self):
        """Run-level disorder summary for the measurement. None when not tracing."""
        if not self._trace_path:
            return None
        events = self.result.disorder_events
        kinds = {}
        for event in events:
            kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
        return {
            "pages": self.result.scanned_pages,
            "disorder_events": len(events),
            "disorder_kinds": kinds,
            "first_disarm_page": (events[0]["page"] if events else None),
            "max_backslide_s": self.result.max_backslide_s,
            "backslides_s": [p["backslide_s"] for p in self.result.page_trace],
            "cleared": self.cleared,
            "stop_reason": self.result.stop_reason,
            "window_reached": self.window_reached,
        }

    def finish(self, stop_reason=None):
        """Record why paging ended. See WindowResult for the vocabulary.

        With no argument, infers it from what actually happened: a run that
        paged past the window covered it; anything else ran out of source or
        budget and must NOT be reported as complete.
        """
        if stop_reason is None:
            stop_reason = "window_cleared" if self.cleared else "source_exhausted"
        self.result.stop_reason = stop_reason
        return self.result

    def as_dict(self):
        return self.result.as_dict()

    def note_line(self):
        return self.result.note_line()

    def reach_line(self):
        return self.result.reach_line()

    def note_reach(self, post_date):
        self.result.note_reach(post_date)

    def budget_unreachable(self, pages_done, budget,
                           calibration=FAILFAST_CALIBRATION_PAGES,
                           margin=FAILFAST_MARGIN):
        """FAIL FAST: can date_from be reached within `budget` pages?

        Answered from the OBSERVED paging velocity, not a guess. The feed is
        newest-first (verified via observe_order), so after a calibration span
        the span of feed-time covered per page is known, and the distance still
        to travel to date_from is known - multiply out and compare to budget.

        Returns True only when the window is CLEARLY out of reach (needs more
        than budget * margin pages), so a realistically reachable window - one
        that needs at most the budget - is never aborted. Records the estimate
        for the diagnostics either way. A no-op (False) until `calibration`
        pages, off a feed with no start bound, or once paging has already
        reached into the window.
        """
        if not self.window or not self.window.start or not budget:
            return False
        if pages_done < calibration:
            return False
        newest = self.result.newest_seen
        reach = self.result.reach_oldest
        if newest is None or reach is None:
            return False
        if reach <= self.window.start:
            return False   # already paged into/past the window - reachable
        self.result.page_budget = budget
        covered = (newest - reach).total_seconds()
        if covered <= 0:
            self.result.estimated_pages_needed = None   # no downward progress
            return True
        per_page = covered / pages_done
        remaining = (reach - self.window.start).total_seconds()
        needed = pages_done + remaining / per_page
        self.result.estimated_pages_needed = int(round(needed))
        # SAY THE ESTIMATE OUT LOUD, once, whether or not we abort on it. The
        # number is already computed here and it is the only early answer to
        # "how long will this take?" - a reachable-but-deep window pages for
        # tens of minutes (measured: a busy group serves ~25 pages/day, so a
        # two-month-old window is ~1,550 pages / ~35 min), and a run that says
        # nothing about that is indistinguishable from a hang.
        if not self._estimate_logged:
            self._estimate_logged = True
            reachable = needed <= budget * margin
            logger.info("[window] after %d page(s): ~%d more page(s) needed to "
                        "reach %s (budget %d) - %s", pages_done,
                        max(0, int(round(needed)) - pages_done),
                        self.window.describe_start(), budget,
                        "continuing" if reachable
                        else "OUT OF REACH, stopping early")
        return needed > budget * margin

    @property
    def window_reached(self):
        return self.result.window_reached
