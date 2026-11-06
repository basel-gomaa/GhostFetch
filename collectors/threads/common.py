"""Paths and limits for the Threads demo collector.

Demo build: the real Threads collector is not part of the public GhostFetch
repository. This package keeps its public API and answers with synthetic
data from ``collectors/demo.py`` - it never contacts Threads.
"""

import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read at import time, like the real collector: a wrapper that wants another
# data directory must set the variable before importing this package.
DATA_DIR = os.environ.get("THREADS_DATA_DIR") or os.path.join(_ROOT, "data", "threads")
STORAGE_STATE_FILE = os.path.join(DATA_DIR, "threads_auth.json")
POSTS_OUTPUT_FILE = os.path.join(DATA_DIR, "threads_posts_raw.json")
COMMENTS_OUTPUT_FILE = os.path.join(DATA_DIR, "threads_comments_raw.json")
COMBINED_OUTPUT_FILE = os.path.join(DATA_DIR, "threads_raw.json")

NUM_POSTS = 50
MAX_POSTS_LIMIT = 1000
MAX_COMMENTS_PER_POST = 1000
PROFILE_COMMENTS_PER_POST = 1000
HEADLESS = True
