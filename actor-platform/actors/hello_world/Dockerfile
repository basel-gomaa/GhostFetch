# hello_world Actor image.
#
# Self-contained by design: the platform builds it, mounts a run directory at
# /actor/storage and starts it. It knows nothing about the platform beyond the
# two paths below.

FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ACTOR_INPUT_PATH=/actor/storage/input.json \
    ACTOR_OUTPUT_PATH=/actor/storage/output.json

WORKDIR /actor

# Dependencies first so code edits don't invalidate the install layer.
# requirements.txt is a LOCK (scripts/image_locks.py): every package pinned
# with its hashes, so pip installs exactly that and refuses anything else.
COPY requirements.txt ./
# pip's download cache is a BuildKit cache MOUNT: it survives rebuilds but is
# never part of the image. So a rebuild downloads only packages whose version
# changed, and the hashes are still checked on every install.
RUN --mount=type=cache,target=/root/.cache/pip pip install --require-hashes --no-deps -r requirements.txt

COPY actor.py ./

# The worker overrides this with the host uid/gid so mounted output files are
# owned by the caller; the declared user keeps a manual `docker run` off root.
RUN useradd --create-home --uid 1000 actor
USER actor

ENTRYPOINT ["python", "actor.py"]
