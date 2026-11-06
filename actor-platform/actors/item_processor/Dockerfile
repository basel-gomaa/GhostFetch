# item_processor Actor image.
#
# Structurally identical to hello_world's Dockerfile even though the Actor is
# very different -- the image only has to satisfy the contract. The one real
# difference is that this one's dependency layer actually installs something.

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
