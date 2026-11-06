#!/usr/bin/env bash
# Vendor the public-groups collector into this Actor's Docker build context.
#
# The scrapers are developed in the pipeline repository and that repository
# stays the SOURCE OF TRUTH. Docker cannot COPY from outside its build context,
# so the import closure is copied in here before `docker build`. Run this after
# any change to the collector; `git status` in the pipeline repo plus a re-run
# is the whole refresh story.
#
# THE CLOSURE IS TWO PACKAGES, and that is the one way this script differs from
# the other five. `collectors/facebook_groups/` is this Actor's collector, and
# it IMPORTS `collectors/facebook/` for every parser it uses - the SSR reader,
# the GraphQL harvester, the record extractor, the cursor plumbing and the
# request builder. It forks none of them, which is the point: a fix to the
# Facebook parsers reaches this Actor by re-running this script, not by being
# applied twice. Add `collectors/daterange.py`, which both import as
# `..daterange`, and the closure is complete - verified by import scan.
#
# The dependency points ONE WAY. Nothing in collectors/facebook imports
# collectors/facebook_groups, so the facebook Actor does NOT vendor this
# package, and its test asserts that. `collectors/facebook/groups/` - a
# different thing entirely, an authenticated multi-group orchestration service -
# is excluded here for the same reason it is excluded there.
#
# THE ANALYSIS AND REPORT LAYER IS VENDORED TOO, for the same reason the
# collectors are: it is developed upstream and must not fork.
#
#     ./sync_collector.sh [path-to-pipeline-repo]
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-${FB_COLLECTOR_SOURCE:-$(cd "$HERE/../../.." && pwd)}}"

for package in facebook facebook_groups; do
    if [[ ! -d "$SOURCE/collectors/$package" ]]; then
        echo "no collector at $SOURCE/collectors/$package" >&2
        echo "pass the pipeline repo path, or set FB_COLLECTOR_SOURCE" >&2
        exit 1
    fi
done

rm -rf "$HERE/vendor"
VENDOR="$HERE/vendor/collectors"
mkdir -p "$VENDOR/facebook" "$VENDOR/facebook_groups"

# Both packages, source only. __pycache__ would bake another machine's bytecode
# into the image.
cp "$SOURCE"/collectors/facebook/*.py "$VENDOR/facebook/"
cp "$SOURCE"/collectors/facebook_groups/*.py "$VENDOR/facebook_groups/"
cp "$SOURCE"/collectors/daterange.py "$VENDOR/"
cp "$SOURCE"/collectors/safe_write.py "$VENDOR/"   # the 0600/0644 writer (N-08)
# Demo build: the stubs answer from collectors/demo.py, which reads its field
# templates from fixtures/demo/ - copied in beside it as demo_fixtures/.
cp "$SOURCE"/collectors/demo.py "$VENDOR/"
rm -rf "$VENDOR/demo_fixtures" && cp -r "$SOURCE"/fixtures/demo "$VENDOR/demo_fixtures"

# `collectors` is a namespace package upstream. Keep it one here too, so the
# vendored modules' `from ..daterange import ...` and `from ..facebook import
# ssr` resolve identically.
find "$VENDOR" -name '__pycache__' -type d -prune -exec rm -rf {} +

# --- the analysis and report layer -----------------------------------------
for pkg in ai reports; do
    mkdir -p "$HERE/vendor/$pkg"
    cp "$SOURCE"/$pkg/*.py "$HERE/vendor/$pkg/"
done

# THE TEXT/PDF RENDERER IS VENDORED UNDER THIS ACTOR'S OWN NAME. It is
# platform-generic -- it switches vocabulary on a `platform=` argument -- and is
# called facebook_report.py upstream only because Facebook came first, so
# shipping that name here would read as the wrong renderer having been sent.
# Contents are untouched; the drift test maps the name back before comparing.
mv "$HERE/vendor/reports/facebook_report.py" \
   "$HERE/vendor/reports/facebook_groups_report.py"

# processors/ IS THE EXCEPTION: only the pair this Actor imports is copied, and
# renamed after the Actor for the same reason the renderer is. A group scrape is
# reshaped by the FACEBOOK processors because the groups collector emits the
# Facebook raw shape deliberately - so this is a rename, never a fork. The drift
# test compares the bytes against the upstream facebook_* pair.
mkdir -p "$HERE/vendor/processors"
for half in page comments; do
    cp "$SOURCE/processors/facebook_${half}_processor.py" \
       "$HERE/vendor/processors/facebook_groups_${half}_processor.py"
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
