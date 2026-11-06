"""
ai/segmentation.py

Responsibility: label EVERY post and EVERY comment with a topic and a
sentiment, so the report can count and cross them.

This is deliberately separate from `ai/claude.py`, which produces the two
RUN-LEVEL analyses (one executive summary, one aggregate sentiment). Those
answer "how did this month go?"; this answers "how many of these complaints are
about claim refusals, and how negative are they?" - which no aggregate can,
because an aggregate has already thrown the per-item detail away.

Two passes, for a reason:

  1. `derive_taxonomy` reads a sample and proposes the topics that are actually
     in the data. A hardcoded topic list would be wrong for every target that
     is not the one it was written for, and a per-batch taxonomy would give
     batch 1 and batch 2 different names for the same thing - so the taxonomy
     is derived ONCE and every batch is labelled against it.
  2. `label_items` assigns each item to one of those topics. Batched, because
     the output is what is large here: one label is ~25 tokens, so a few
     hundred items would silently truncate against ANALYSIS_MAX_TOKENS.

Failure is never fatal. Segmentation enriches a report; it does not gate one.
Every entry point returns None on failure and logs, exactly like the Gmail step.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock

from .claude import MODEL, ANALYSIS_MAX_TOKENS, client

logger = logging.getLogger("ai.segmentation")

# Items per labelling call, and the budget each call gets.
#
# The binding constraint is THINKING, not the answer. Measured on a 120-item
# batch: `stop_reason: max_tokens` with output_tokens 8000 of which 7999 were
# thinking_tokens - the model reasoned through the classifications until the
# budget was gone and never emitted a single label. The response carried one
# empty `thinking` block and no text at all, which is why the failure surfaced
# as an exception with an EMPTY message rather than as truncated JSON.
#
# So the batch is sized against reasoning (~65 thinking tokens per item, so 60
# items is ~4k) and given headroom on top of it. Both numbers matter: raising
# the budget alone still loses batches on a hard set, and shrinking the batch
# alone multiplies the number of calls.
BATCH_SIZE = 60
SEGMENT_MAX_TOKENS = 16000

# Concurrent labelling calls. Deliberately small - this is one account's API
# budget, and a batch that overruns splits into more calls of its own.
LABEL_CONCURRENCY = 3

# Items shown to the taxonomy pass. It only needs enough to see the themes, not
# the whole corpus, and a long taxonomy prompt buys nothing.
TAXONOMY_SAMPLE = 220

# Items LABELLED in one run - the twin of `claude.MAX_COMMENTS_IN_PROMPT`, and
# the reason this exists is that the analysis prompt had a cap and this did not.
#
# Cost here is linear in items and in nothing else: every item is a decision the
# model reasons about (~57 input tokens, ~90 output including thinking) and the
# batches are just that divided by 60. So a run's segmentation bill scales with
# the scrape while the two run-level analyses stay flat at their own cap.
# MEASURED on the 2,594-post / 3,484-comment group run: $7.79 of Claude spend,
# of which the two capped analyses were $1.12 and this uncapped pass was $6.67.
#
# 1,500 matches the analysis cap deliberately - the same "enough to characterise
# the material" judgement, and a run whose report says 1,500 in one section and
# 6,078 in another invites the question of which one is the sample.
MAX_ITEMS_TO_LABEL = 1500

# Characters of any one item shown to the model. Long enough to classify by,
# short enough that a handful of essay-length posts cannot crowd out a batch.
TEXT_LIMIT = 400

SENTIMENTS = ("Positive", "Neutral", "Negative", "Mixed")

SYSTEM_PROMPT = (
    "You are a social media analyst segmenting posts and comments. Work in "
    "whatever language the text is written in (Arabic and English are both "
    "common) but always write topic names and labels in English. Judge only "
    "what the text actually says - never infer sentiment from the topic being "
    "a negative subject. A calm factual question about a complaint topic is "
    "Neutral, not Negative."
)

# NOTE: every "object" here sets additionalProperties: false explicitly. The
# structured-output API requires it on EACH object, nested ones included, and
# rejects the whole request with a 400 otherwise.
_TAXONOMY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short topic name, 2-4 words, in English.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One sentence describing what belongs "
                                       "in this topic.",
                    },
                },
                "required": ["name", "description"],
            },
            "description": "Between 4 and 9 distinct, non-overlapping topics "
                           "that together cover the material. Prefer specific "
                           "topics over generic ones like 'General' or "
                           "'Other'.",
        },
    },
    "required": ["topics"],
}

_LABEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The exact id given for this item. "
                                       "Never invent one.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Exactly one topic name from the list "
                                       "provided.",
                    },
                    "sentiment": {"type": "string", "enum": list(SENTIMENTS)},
                    "sentiment_score": {
                        "type": "number",
                        "description": "-1.0 (very negative) to 1.0 (very "
                                       "positive).",
                    },
                },
                "required": ["id", "topic", "sentiment", "sentiment_score"],
            },
            "description": "Exactly one entry for every item given, in the "
                           "same order.",
        },
    },
    "required": ["labels"],
}


@dataclass
class SegmentationResult:
    """Per-item labels plus the rollups the report actually renders."""
    topics: list[dict] = field(default_factory=list)      # name/description/count/sentiment
    labels: dict = field(default_factory=dict)            # item id -> label dict
    sentiment_totals: dict = field(default_factory=dict)  # sentiment -> count
    post_count: int = 0
    comment_count: int = 0
    unlabelled: int = 0                                   # items the model skipped
    # Items never SENT, because the run exceeded MAX_ITEMS_TO_LABEL. Separate
    # from `unlabelled` on purpose: one is a deliberate budget and the other is
    # a batch that failed, and reporting a sample as a failure (or the reverse)
    # misreads the run in opposite directions.
    not_sampled: int = 0
    total_items: int = 0                                  # the denominator

    def __bool__(self) -> bool:
        return bool(self.labels)


def _clip(text) -> str:
    text = " ".join(str(text or "").split())
    return text[:TEXT_LIMIT]


def _call(schema, instruction: str, payload: str, max_tokens=None):
    if client is None:
        raise RuntimeError("no ANTHROPIC_API_KEY - use the offline demo analysis")
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens or ANALYSIS_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": f"{instruction}\n\n{payload}"}],
    )
    # Say WHICH way it failed. A response that spent its whole budget thinking
    # carries no text block at all, so the generic parser below raises a bare
    # StopIteration with an empty message - which is exactly as informative as
    # silence, and is what made this take a probe to diagnose.
    if not any(block.type == "text" for block in response.content):
        usage = response.usage
        raise RuntimeError(
            f"no answer returned (stop_reason={response.stop_reason}, "
            f"output_tokens={usage.output_tokens}, thinking_tokens="
            f"{getattr(usage.output_tokens_details, 'thinking_tokens', '?')}) "
            "- the batch is too large for the token budget")
    from .claude import _parse_structured_json
    return _parse_structured_json(response)


# ---------------------------------------------------------------------------
# Collecting the items to label
# ---------------------------------------------------------------------------

def collect_items(page_data: dict, comments_data: dict) -> list[dict]:
    """Flatten posts + comments (+ replies) into one labelling worklist.

    The id is whatever already identifies the item - `post_url` for a post,
    `comment_id` for a comment - so the labels join back onto the processed
    documents without inventing a parallel identifier scheme. An item with no
    id or no text cannot be labelled or joined and is left out.

    EVERY item is returned; `sample_items` is what applies the budget. This
    stays the complete worklist because it is also the denominator - "1,500 of
    6,078" cannot be reported by something that only ever saw 1,500.

    `post` and `thread` are carried for the sampler alone (which post an item
    belongs to, and which top-level comment it hangs off - a reply's thread is
    its parent's id). `likes` orders that sampling. None of the three reach the
    model: `_render` sends id, kind and text.
    """
    items = []
    for post in (page_data or {}).get("posts") or []:
        text, url = _clip(post.get("caption")), post.get("post_url")
        if text and url:
            items.append({"id": url, "kind": "post", "text": text})

    seen = set()
    for index, post in enumerate((comments_data or {}).get("posts") or []):
        for comment in post.get("comments") or []:
            parent = comment.get("comment_id")
            for record in [comment] + list(comment.get("reply_comments") or []):
                cid, text = record.get("comment_id"), _clip(record.get("comment"))
                if not cid or not text or cid in seen:
                    continue
                seen.add(cid)
                items.append({"id": cid, "kind": "comment", "text": text,
                              "post": index, "thread": parent or cid,
                              "likes": comment.get("likes") or 0})
    return items


def sample_items(items: list[dict], limit: int = MAX_ITEMS_TO_LABEL) -> list[dict]:
    """Cut a run's worklist down to `limit` items, fairly.

    A NO-OP below the limit - the list is returned unchanged, same order - so
    every ordinary run labels everything exactly as before and only a run big
    enough to be worth capping is touched.

    Posts get at most half the budget and comments the rest, because either
    population can swamp the other: a 2,594-post group would otherwise leave no
    room for a single comment, and one viral post's 5,000 replies would leave
    no room for the other 99 posts. The half is a floor for comments only when
    there are comments to fill it: whatever they cannot use goes back to the
    posts, so a post-heavy run with few comments still spends its budget.

    WHY ROUND-ROBIN RATHER THAN A PER-POST CAP. `_format_facebook_comments_for_prompt`
    divides its budget into a per-post cap, which works there because it needs
    no hard total - it just stops printing. Here the total IS the cost, so the
    allocation has to both be fair and stop. Taking each post's highest-liked
    thread, then everyone's second, and so on until the budget runs out, does
    both: truncation lands on the least-liked thread of every post rather than
    dropping the last posts entirely, which is what concatenating and cutting
    would do. A thread is kept WHOLE - a reply labelled with no parent is a
    label nothing can be read against.

    The one case where posts DO miss out is a budget smaller than the number of
    posts - rank 0 alone spends it, and the posts past that point contribute no
    comments. Nothing can be done about that (someone has to miss out), and the
    ones dropped are the lowest in the document's own order, which for Facebook
    is engagement-ranked. Measured on a 2,594-post / 5,400-comment corpus: 1,500
    items kept, 375 of 900 commented posts represented.
    """
    if len(items) <= limit:
        return items

    posts = [i for i in items if i["kind"] == "post"]
    comments = [i for i in items if i["kind"] != "post"]
    post_share = max(limit // 2, limit - len(comments), 1)
    kept = posts[:post_share]
    budget = limit - len(kept)

    threads: dict = {}
    for item in comments:
        threads.setdefault(item.get("post"), {}).setdefault(
            item.get("thread") or item["id"], []).append(item)
    # Highest-liked thread first within each post; posts in their own order.
    ranked = [sorted(post.values(), key=lambda t: -(t[0].get("likes") or 0))
              for post in threads.values()]

    taken: list[dict] = []
    for rank in range(max((len(p) for p in ranked), default=0)):
        for post_threads in ranked:
            if rank >= len(post_threads):
                continue
            thread = post_threads[rank]
            if len(taken) + len(thread) <= budget:
                taken.extend(thread)
        if len(taken) >= budget:
            break
    return kept + taken


def _render(items: list[dict]) -> str:
    return "\n".join(f"[{item['id']}] ({item['kind']}) {item['text']}"
                     for item in items)


# ---------------------------------------------------------------------------
# The two passes
# ---------------------------------------------------------------------------

def derive_taxonomy(items: list[dict]) -> list[dict]:
    """Propose the topics present in this material. One call."""
    if client is None:
        from . import demo_analysis
        return demo_analysis.taxonomy()
    sample = items[:TAXONOMY_SAMPLE]
    data = _call(
        _TAXONOMY_SCHEMA,
        "Read these social media posts and comments and identify the distinct "
        "topics they discuss. Return topics that describe THIS material "
        "specifically - a reader should be able to tell what the conversation "
        "is about from the topic names alone.",
        _render(sample),
    )
    topics = [t for t in data.get("topics") or []
              if isinstance(t, dict) and t.get("name")]
    logger.info("Segmentation taxonomy: %s",
                ", ".join(t["name"] for t in topics) or "none")
    return topics


def label_items(items: list[dict], topics: list[dict]) -> dict:
    """Label every item against ``topics``. Batched; partial results are kept.

    Ids the model returns that were not in the batch are DROPPED rather than
    trusted - a hallucinated id would otherwise attach a label to nothing, or
    worse, silently to the wrong item. A batch that fails outright leaves its
    items unlabelled and the rest of the run continues.
    """
    if client is None:
        from . import demo_analysis
        return demo_analysis.label(items)
    topic_names = {t["name"] for t in topics}
    topic_block = "\n".join(f"- {t['name']}: {t.get('description', '')}"
                            for t in topics)
    labels = {}
    lock = Lock()

    def label_batch(batch, depth=0):
        """Label one batch, HALVING and retrying once if it overruns.

        The failure mode this recovers from is budget exhaustion, which scales
        with how many decisions the batch asks for - so splitting it is the
        actual remedy, not a retry of the same request. Bounded to two splits
        so a genuinely broken call cannot fan out.
        """
        try:
            data = _call(
                _LABEL_SCHEMA,
                "Assign each item below exactly one topic from this list and "
                "one sentiment. Use the item's id verbatim.\n\nTOPICS:\n"
                f"{topic_block}",
                _render(batch),
                max_tokens=SEGMENT_MAX_TOKENS,
            )
        except Exception as exc:
            if depth < 2 and len(batch) > 8:
                half = len(batch) // 2
                logger.info("Segmentation batch of %d overran (%s) - splitting",
                            len(batch), exc)
                label_batch(batch[:half], depth + 1)
                label_batch(batch[half:], depth + 1)
                return
            logger.warning("Segmentation batch of %d failed (%s) - those items "
                           "stay unlabelled", len(batch), exc)
            return
        _accept(data, {item["id"] for item in batch})

    def _accept(data, valid_ids):
        for label in data.get("labels") or []:
            if not isinstance(label, dict):
                continue
            item_id = str(label.get("id") or "")
            # Both guards matter: an unknown id means the model invented one,
            # and an unknown topic means it invented a category outside the
            # taxonomy every other batch was labelled against.
            if item_id not in valid_ids:
                continue
            topic = label.get("topic")
            if topic not in topic_names:
                continue
            sentiment = label.get("sentiment")
            if sentiment not in SENTIMENTS:
                sentiment = "Neutral"
            score = label.get("sentiment_score")
            with lock:
                labels[item_id] = {
                    "topic": topic,
                    "sentiment": sentiment,
                    "sentiment_score": (round(float(score), 2)
                                        if isinstance(score, (int, float)) else None),
                }

    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    # Batches are independent - each one only reads the shared taxonomy and
    # writes its own ids - so they run concurrently. Sequentially a 285-item
    # run is five calls of a minute or more, which is several minutes added to
    # every report. Kept small: this is one account's API budget, and the
    # split-on-overrun path can turn one batch into several.
    with ThreadPoolExecutor(max_workers=LABEL_CONCURRENCY) as pool:
        list(pool.map(label_batch, batches))
    return labels


def _rollups(items: list[dict], topics: list[dict], labels: dict
             ) -> tuple[list[dict], dict]:
    by_kind = {item["id"]: item["kind"] for item in items}
    counts = {t["name"]: {"name": t["name"],
                          "description": t.get("description", ""),
                          "count": 0, "posts": 0, "comments": 0,
                          **{s: 0 for s in SENTIMENTS}}
              for t in topics}
    totals = {s: 0 for s in SENTIMENTS}

    for item_id, label in labels.items():
        row = counts.get(label["topic"])
        if row is None:
            continue
        row["count"] += 1
        row["posts" if by_kind.get(item_id) == "post" else "comments"] += 1
        row[label["sentiment"]] += 1
        totals[label["sentiment"]] += 1

    ranked = sorted(counts.values(), key=lambda r: r["count"], reverse=True)
    return [r for r in ranked if r["count"]], totals


def segment(page_data: dict, comments_data: dict):
    """Label everything and roll it up. Returns None when there is nothing to
    segment or the analysis fails - never raises into the pipeline."""
    everything = collect_items(page_data, comments_data)
    if not everything:
        logger.info("Nothing to segment (no captions or comments with ids)")
        return None
    items = sample_items(everything)
    if len(items) < len(everything):
        logger.info("Segmenting a sample of %d of %d item(s) - highest-liked "
                    "threads, spread across posts (MAX_ITEMS_TO_LABEL=%d)",
                    len(items), len(everything), MAX_ITEMS_TO_LABEL)
    try:
        topics = derive_taxonomy(items)
        if not topics:
            logger.warning("Segmentation produced no topics - skipping")
            return None
        labels = label_items(items, topics)
    except Exception as exc:
        logger.warning("Segmentation unavailable (%s) - the report is built "
                       "without it", exc)
        return None

    ranked, totals = _rollups(items, topics, labels)
    result = SegmentationResult(
        topics=ranked,
        labels=labels,
        sentiment_totals=totals,
        post_count=sum(1 for i in items if i["kind"] == "post" and i["id"] in labels),
        comment_count=sum(1 for i in items
                          if i["kind"] == "comment" and i["id"] in labels),
        unlabelled=sum(1 for i in items if i["id"] not in labels),
        not_sampled=len(everything) - len(items),
        total_items=len(everything),
    )
    logger.info("Segmented %d post(s) + %d comment(s) into %d topic(s)%s%s",
                result.post_count, result.comment_count, len(ranked),
                f"; {result.unlabelled} unlabelled" if result.unlabelled else "",
                f"; {result.not_sampled} of {result.total_items} not sampled"
                if result.not_sampled else "")
    return result


def apply_labels(page_data: dict, comments_data: dict, result) -> None:
    """Write the labels back onto the processed documents, in place.

    Additive: an item the model skipped simply has no `topic`/`sentiment` key,
    and a run without segmentation leaves both documents untouched - which is
    what keeps every other platform's output byte-identical.
    """
    if not result:
        return
    labels = result.labels

    for post in (page_data or {}).get("posts") or []:
        post.update(labels.get(post.get("post_url"), {}))
    for post in (comments_data or {}).get("posts") or []:
        for comment in post.get("comments") or []:
            comment.update(labels.get(comment.get("comment_id"), {}))
            for reply in comment.get("reply_comments") or []:
                reply.update(labels.get(reply.get("comment_id"), {}))
