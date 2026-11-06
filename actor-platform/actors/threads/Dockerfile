# threads Actor image.
#
# Carries the EXISTING Threads collector, vendored into vendor/ by
# sync_collector.sh (Docker cannot COPY from outside its build context, and the
# collector is developed in the pipeline repository). Run that script before
# building.
#
# Nothing about the platform is in here -- no registry, no queue, no API. The
# image holds one Actor and its own dependencies, as every other Actor does.
#
# SLIM, LIKE THE FACEBOOK IMAGE AND UNLIKE THE OTHER THREE. This collector needs
# no browser on any path: both its tiers are plain HTTP, there is deliberately
# no in-page fetch tier (the endpoint is unsigned, so a browser would buy
# nothing) and no DOM tier (every field the reports need is already JSON).
# Playwright appears once in the whole package, in a rescue path for a rotated
# query id that logs a warning and continues when it is absent. ~180MB.

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
