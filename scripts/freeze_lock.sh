#!/usr/bin/env bash
# Rewrite requirements.lock.txt (pipeline) and actor-platform/requirements.lock.txt
# (platform) from the two working virtualenvs. Run after an intentional upgrade,
# once the tests pass on it. Neither venv needs pip: this reads installed
# package metadata directly.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FREEZE='import importlib.metadata as m
skip={"pip","setuptools","wheel"}
rows=sorted({(d.metadata["Name"].lower().replace("_","-"),d.version) for d in m.distributions() if d.metadata["Name"] and d.metadata["Name"].lower() not in skip})
print("\n".join(f"{n}=={v}" for n,v in rows))'
lock() {  # <python> <lockfile> <install hint>
    { echo "# Exact versions last known to work, recorded $(date +%F) by scripts/freeze_lock.sh."
      echo "# requirements.txt says what we depend on; install from THIS file:"
      echo "#     $3"
      "$1" -c "$FREEZE"; } > "$2"
    echo "wrote $2 ($(grep -vc '^#' "$2") packages)"
}
lock "$ROOT/.venv/bin/python" "$ROOT/requirements.lock.txt" ".venv/bin/pip install -r requirements.lock.txt"
lock "$ROOT/actor-platform/.venv/bin/python" "$ROOT/actor-platform/requirements.lock.txt" \
     ".venv/bin/python -m pip install -r requirements.lock.txt"
