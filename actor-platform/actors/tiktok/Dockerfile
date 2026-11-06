# tiktok Actor image (GhostFetch demo build).
#
# Carries the TikTok collector, vendored into vendor/ by sync_collector.sh
# (Docker cannot COPY from outside its build context). In the public demo
# build that collector returns synthetic data, so this image needs no browser,
# no virtual display and no VNC viewer - it is a plain Python image like the
# other Actors.
FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ACTOR_INPUT_PATH=/actor/storage/input.json \
    ACTOR_OUTPUT_PATH=/actor/storage/output.json \
    TIKTOK_HEADLESS=1 \
    HOME=/tmp

WORKDIR /actor

# requirements.txt is a LOCK (scripts/image_locks.py): every package pinned
# with its hashes, so pip installs exactly that and refuses anything else.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --require-hashes --no-deps -r requirements.txt

# `collectors` is a NAMESPACE package (no __init__.py), which is what makes the
# vendored modules' `from ..daterange import ...` resolve as in the repository.
COPY vendor/collectors ./collectors
COPY vendor/processors ./processors
COPY vendor/ai ./ai
COPY vendor/reports ./reports
COPY vendor/config.py ./

COPY actor.py ./

# The worker overrides this with the host uid/gid so mounted output files are
# owned by the caller; the declared user keeps a manual `docker run` off root.
RUN useradd --create-home --uid 1000 actor
USER actor

ENTRYPOINT ["python", "actor.py"]
