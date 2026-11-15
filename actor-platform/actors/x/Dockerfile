# x Actor image.
#
# Carries the EXISTING X collector, vendored into vendor/ by
# sync_collector.sh (Docker cannot COPY from outside its build context, and the
# collector is developed in the pipeline repository). Run that script before
# building.
#
# Nothing about the platform is in here -- no registry, no queue, no API. The
# image holds one Actor and its own dependencies, as every other Actor does.
#
# BASED ON THE PLAYWRIGHT IMAGE, unlike the facebook Actor's python:3.13-slim.
# MOSTLY unnecessary here, and carried anyway on purpose. This collector's
# profile and reply paths are pure HTTP -- its API is unsigned, so there is no
# in-page tier and no DOM tier at all. Only SEARCH needs a browser, because that
# endpoint requires a signed header only X's own page can mint.
#
# The slim base would therefore work for two modes out of three. It is not used
# because the cost of this one is nearly zero: the Playwright base is already
# pulled for two other Actors, and MEASURED with `docker system df -v`, each of
# those adds only ~192MB unique on top of 2.445GB of shared layers. Paying ~190MB
# to keep search working beats shipping a mode that fails.
# Using Microsoft's own image rather than installing Chromium by hand is what
# keeps the system libraries and the browser build in step -- a hand-rolled
# install drifts and fails at run time, inside a container, with a stack trace
# about a missing shared object.
FROM mcr.microsoft.com/playwright/python:v1.65.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ACTOR_INPUT_PATH=/actor/storage/input.json \
    ACTOR_OUTPUT_PATH=/actor/storage/output.json

WORKDIR /actor

# THE BASE IMAGE'S OWN PYTHON PACKAGES GO FIRST. It pip-installs virtualenv and
# what virtualenv needs (filelock, distlib, platformdirs, ...); nothing here
# runs them, no lock file listed them, and on 30 October they carried 24
# advisories that every audit missed. Removing whatever the base installed
# leaves this image's Python packages exactly pip plus the lock below, which
# is what `scripts/image_locks.py audit` checks.
RUN python3 -m pip freeze --exclude pip \
        | xargs -r python3 -m pip uninstall -y --break-system-packages

# Dependencies first so code edits don't invalidate the install layer.
# requirements.txt is a LOCK (scripts/image_locks.py): every package pinned
# with its hashes, so pip installs exactly that and refuses anything else.
COPY requirements.txt ./
# pip's download cache is a BuildKit cache MOUNT: it survives rebuilds but is
# never part of the image. So a rebuild downloads only packages whose version
# changed, and the hashes are still checked on every install.
RUN --mount=type=cache,target=/root/.cache/pip pip install --break-system-packages --require-hashes --no-deps -r requirements.txt

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
# owned by the caller. The base image already ships a non-root `pwuser` that
# owns the browser install, so it is reused rather than a second user created.
USER pwuser

ENTRYPOINT ["python", "actor.py"]
