"""The comment-stage work list (demo build)."""

from dataclasses import dataclass


@dataclass
class Stage:
    """What the post queue held when the comment stage started."""
    name: str
    status: str
    queued: int

    def as_dict(self):
        return {"stage": self.name, "status": self.status, "queued": self.queued}


def worklist(posts_doc):
    """Every post with a URL. -> (urls, Stage)."""
    urls = [p["post_url"] for p in (posts_doc or {}).get("posts") or []
            if p.get("post_url")]
    return urls, Stage("post_queue", "success", len(urls))
