#!/usr/bin/env bash
# Vendor the TikTok collector into this Actor's Docker build context.
#
# The scraper is developed in the pipeline repository and that repository stays
# the SOURCE OF TRUTH. Docker cannot COPY from outside its build context, so the
# import closure is copied in here before `docker build`. Run this after any
# change to the collector; `git status` in the pipeline repo plus a re-run is
# the whole refresh story.
#
# THE COLLECTOR CLOSURE IS EXACTLY TWO THINGS, verified by import scan rather
# than guessed: collectors/tiktok/*.py (14 modules) and collectors/daterange.py,
# which collectors/tiktok/engine.py and __init__.py import as `..daterange`.
# `collectors/` is a namespace package with no __init__.py, so nothing else in
# it is pulled in -- no Facebook, no Instagram, and no main.py.
#
# THE ANALYSIS AND REPORT LAYER IS VENDORED TOO, and for the same reason the
# collector is: it is developed upstream and must not fork. Its closure is
# processors/ (pure reshaping), ai/ (the Claude calls) and reports/*.py (text,
# PDF and dashboard), plus config.py, which ai/claude.py imports for the API
# key. reports/output/ is EXCLUDED -- it is generated artifacts, megabytes of
# them, and an Actor writes its own into the run directory.
#
# That layer is deliberately NOT rewritten per platform: reports/ is already
# platform-generic (it takes a `platform` string), so vendoring it once is what
# lets the next six Actors reuse it rather than each growing their own.
#
# collectors/tiktok/groups/ IS DELIBERATELY EXCLUDED, and the direction of
# the dependency is why: it IMPORTS the collector (a multi-group orchestration
# service that spawns its own worker processes), and nothing in the collector
# imports it. Vendoring it would put a second execution engine inside an Actor
# whose execution this platform already owns.
#
#     ./sync_collector.sh [path-to-pipeline-repo]
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-${TIKTOK_COLLECTOR_SOURCE:-$(cd "$HERE/../../.." && pwd)}}"

if [[ ! -d "$SOURCE/collectors/tiktok" ]]; then
    echo "no collector at $SOURCE/collectors/tiktok" >&2
    echo "pass the pipeline repo path, or set TIKTOK_COLLECTOR_SOURCE" >&2
    exit 1
fi

rm -rf "$HERE/vendor"
VENDOR="$HERE/vendor/collectors"
mkdir -p "$VENDOR/tiktok"

# The package itself, source only. __pycache__ would bake another machine's
# bytecode into the image.
cp "$SOURCE"/collectors/tiktok/*.py "$VENDOR/tiktok/"
cp "$SOURCE"/collectors/daterange.py "$VENDOR/"
cp "$SOURCE"/collectors/safe_write.py "$VENDOR/"   # the 0600/0644 writer (N-08)
# Demo build: the stubs answer from collectors/demo.py, which reads its field
# templates from fixtures/demo/ - copied in beside it as demo_fixtures/.
cp "$SOURCE"/collectors/demo.py "$VENDOR/"
rm -rf "$VENDOR/demo_fixtures" && cp -r "$SOURCE"/fixtures/demo "$VENDOR/demo_fixtures"

# `collectors` is a namespace package upstream. Keep it one here too, so the
# vendored modules' `from ..daterange import ...` resolves identically.
find "$VENDOR" -name '__pycache__' -type d -prune -exec rm -rf {} +

# --- the analysis and report layer -----------------------------------------
# Source only, and no generated output. Each is a real package upstream (they
# have __init__.py or are imported as `ai.claude` / `reports.common`), so the
# directory names are preserved exactly.
for pkg in ai reports; do
    mkdir -p "$HERE/vendor/$pkg"
    cp "$SOURCE"/$pkg/*.py "$HERE/vendor/$pkg/"
done

# THE TEXT/PDF RENDERER IS COPIED UNDER THIS ACTOR'S OWN NAME. Upstream calls it
# facebook_report.py -- it is platform-generic (it switches vocabulary on a
# `platform=` argument) and every Actor uses it, so the name is historical and
# reads as a mistake in any other Actor's vendor tree: "tiktok/vendor/reports/
# facebook_report.py" invites the reader to think the wrong renderer was
# shipped. The CONTENTS are untouched and the drift test still compares them
# byte for byte against upstream; only the filename follows the Actor.
mv "$HERE/vendor/reports/facebook_report.py" "$HERE/vendor/reports/tiktok_report.py"

# processors/ IS THE EXCEPTION: only THIS platform's pair is copied. Upstream
# holds twelve (six platforms x page/comments) and actor.py imports exactly two
# -- the other ten were dead weight in every image, and an image carrying
# another platform's reshaping code invites someone to import it. They are
# independent modules: no processor imports another, so taking two is the whole
# closure. The drift test pins this subset, so adding a platform upstream does
# not silently start shipping it here.
mkdir -p "$HERE/vendor/processors"
for half in page comments; do
    cp "$SOURCE/processors/tiktok_${half}_processor.py" "$HERE/vendor/processors/"
done

# ai/claude.py does `from config import ANTHROPIC_API_KEY` at import time, so
# the module has to be there for the package to load at all.
cp "$SOURCE"/config.py "$HERE/vendor/"

find "$HERE/vendor" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$HERE/vendor/SOURCE.txt" <<EOF
Vendored from: $SOURCE
On:            $(date -u +%Y-%m-%dT%H:%M:%SZ)
Commit:        $(git -C "$SOURCE" rev-parse --short HEAD 2>/dev/null || echo 'not a git repo')
Branch:        $(git -C "$SOURCE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '-')

Do not edit anything under vendor/ -- it is overwritten by sync_collector.sh.
Change the scraper in the pipeline repository and re-run the script.
EOF

echo "vendored $(find "$HERE/vendor" -name '*.py' | wc -l) module(s), $(find "$HERE/vendor" -name '*.py' -exec cat {} + | wc -l) lines"
cat "$HERE/vendor/SOURCE.txt"
