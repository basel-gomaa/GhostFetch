"""YouTube collector (demo build).

Demo build: the real collector called the YouTube Data API. This one returns a
few synthetic videos and comments in the same dataclass shapes, so the YouTube
report path runs without an API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import demo


@dataclass
class Comment:
    comment_id: str
    author: str
    text: str
    like_count: int
    published_at: str


@dataclass
class Video:
    video_id: str
    title: str
    published_at: str
    description: str = ""
    comments: list[Comment] = field(default_factory=list)


def collect_channel_data(channel_id: str, video_count: int = 5,
                         comments_per_video: int = 20, window=None) -> list[Video]:
    rng = demo._rng("youtube", channel_id)
    now = datetime.now(timezone.utc)
    videos = []
    for i in range(video_count):
        published = now - timedelta(days=3 * i + 1)
        if window and not window.contains(published.isoformat()):
            continue
        vid = demo._id("youtube", channel_id, i, length=11)
        video = Video(video_id=vid, title=demo.CAPTIONS[i % len(demo.CAPTIONS)],
                      published_at=published.isoformat(),
                      description="Synthetic demo video.")
        for j in range(min(comments_per_video, rng.randint(4, 9))):
            text, _ = demo.COMMENTS[rng.randrange(len(demo.COMMENTS))]
            video.comments.append(Comment(
                comment_id=demo._id(vid, j), author=f"viewer{rng.randint(1, 999)}",
                text=text, like_count=rng.randint(0, 80),
                published_at=(published + timedelta(hours=j + 1)).isoformat()))
        videos.append(video)
    return videos
