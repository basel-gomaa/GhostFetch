# facebook_groups Actor image.
#
# Carries the EXISTING collectors, vendored into vendor/ by sync_collector.sh
# (Docker cannot COPY from outside its build context, and they are developed in
# the pipeline repository). Run that script before building.
#
# TWO collector packages, not one: collectors/facebook_groups is this Actor's,
# and collectors/facebook is its dependency - the groups collector imports that
# one's parsers rather than forking them. One COPY line picks up both, because
# they are siblings under vendor/collectors/.
#
# Nothing about the platform is in here -- no registry, no queue, no API. The
# image holds one Actor and its own dependencies, as every other Actor does.
#
# SLIM. This Actor needs no browser on any path: it is plain HTTP end to end,
# and there is NO SESSION anywhere in it - no cookie jar is read, written or
# forwarded, because everything it collects is public. The vendored Facebook
# package imports Playwright only lazily, in paths this Actor never takes, so
# nothing pulls a browser into the image.

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

# The collector, then the adapter. `collectors` is a NAMESPACE package (no
# __init__.py) both here and upstream, which is what makes the vendored
# modules' `from ..daterange import ...` resolve exactly as it does in the
# pipeline repository.
COPY vendor/collectors ./collectors

# The analysis and report layer, vendored from the same upstream repository.
# config.py sits at the import root because ai/claude.py does `from config
# import ANTHROPIC_API_KEY` -- an absolute import, so the module has to be
# importable as a top-level name.
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
