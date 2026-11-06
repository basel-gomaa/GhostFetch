#!/usr/bin/env bash
# Start the whole thing locally: database, API and one Worker.
#
#     ./run-local.sh          start everything, then open http://127.0.0.1:8000
#     ./run-local.sh stop     stop the API and Worker (the database keeps running)
#
#     PUBLIC_DOMAIN=platform.example.com ./run-local.sh
#                             the same, served PUBLICLY over HTTPS: Caddy fronts it
#                             with an automatic certificate (deploy/Caddyfile), the
#                             API binds to 127.0.0.1 only, and plain-HTTP sign-in
#                             is refused. Needs that domain's DNS pointing here and
#                             ports 80/443 open.
#
# Settings live in .env.local, which is gitignored and holds a real encryption
# key. It is created on first run and MUST be kept: it decrypts every stored
# social session, so replacing it means everyone reconnects.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PORT="${PORT:-8000}"
LOGS="${LOGS:-/tmp/actor-platform}"
mkdir -p "$LOGS"

if [[ "${1:-}" == "stop" ]]; then
    pkill -f "uvicorn api.main:app" 2>/dev/null || true
    pkill -f "worker/consumer.py" 2>/dev/null || true
    echo "stopped. The database is still running (docker compose --profile db down to stop it)."
    exit 0
fi

[[ -f .env.local ]] || { echo "no .env.local - copy .env.example and see the README's Multi-tenancy section" >&2; exit 1; }
# Owner-only: it holds the key that decrypts every stored session, the database
# password and the operator key. It was world-readable (0755) on this machine.
chmod 600 .env.local
[[ -f ../.env ]] && chmod 600 ../.env
# docker compose reads ./.env by itself for ${VARS} in docker-compose.yml, so
# linking it to .env.local lets a bare `docker compose --profile db down` find
# POSTGRES_PASSWORD, which no longer has a default. Never replaces a real file.
[[ -e .env || -L .env ]] || ln -s .env.local .env
set -a; source .env.local; set +a

# ONE-TIME: REPLACE THE DEFAULT DATABASE PASSWORD (security review M-03).
# docker-compose.yml no longer falls back to the well-known "actorplatform".
# An install made before that still uses it, so -- only when .env.local has no
# POSTGRES_PASSWORD and its database URL still carries the old default -- this
# generates a new one, sets it INSIDE the running database (SQL on stdin, so the
# value is never in any process's argv), and rewrites only the URL's password in
# .env.local after keeping a copy as .env.local.bak. Users, runs and stored
# sessions are untouched; the encryption key line is never edited.
PG_PORT="${POSTGRES_PORT:-5433}"
if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
    if [[ "${ACTOR_PLATFORM_DATABASE_URL:-}" == *"actorplatform:actorplatform@127.0.0.1:${PG_PORT}/"* ]]; then
        echo "==> database password (one-time: replacing the old default with a generated one)"
        NEW_PG_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
        # The volume already exists, so this value is ignored by postgres at
        # start-up; it only satisfies compose until the ALTER below.
        export POSTGRES_PASSWORD="$NEW_PG_PASSWORD"
        docker compose --profile db up -d postgres >/dev/null
        until docker compose --profile db exec -T postgres pg_isready -U actorplatform -q 2>/dev/null; do sleep 1; done
        printf "ALTER USER actorplatform WITH PASSWORD '%s';\n" "$NEW_PG_PASSWORD" |
            docker compose --profile db exec -T postgres psql -U actorplatform -d actorplatform -q -v ON_ERROR_STOP=1 >/dev/null
        cp -p .env.local .env.local.bak && chmod 600 .env.local.bak
        NEW_PG_PASSWORD="$NEW_PG_PASSWORD" python3 - <<'PY'
import os
from pathlib import Path
new = os.environ["NEW_PG_PASSWORD"]
p = Path(".env.local")
lines = p.read_text().splitlines()
out = []
for line in lines:
    if line.startswith("ACTOR_PLATFORM_DATABASE_URL="):
        line = line.replace("actorplatform:actorplatform@", f"actorplatform:{new}@", 1)
    out.append(line)
out.append(f"POSTGRES_PASSWORD={new}")
p.write_text("\n".join(out) + "\n")
PY
        set -a; source .env.local; set +a
        echo "    done; the previous .env.local is kept as .env.local.bak"
    else
        echo "POSTGRES_PASSWORD is not set in .env.local. Add it (it must match the password in" >&2
        echo "ACTOR_PLATFORM_DATABASE_URL), e.g.: POSTGRES_PASSWORD=<the database password>" >&2
        exit 1
    fi
fi

# WHERE THE API LISTENS. Loopback by default (security review N-01).
#
# It used to bind 0.0.0.0, which put the API on every interface -- the LAN, and
# the Docker bridge gateways (172.19.0.1 / 172.20.0.1) that every Actor
# container can reach. Nothing needs it there: the console is served by the API
# itself, Caddy proxies over 127.0.0.1, and no container ever calls the API
# (Actors hand results back through files in their run directory).
#
# ACTOR_PLATFORM_BIND_HOST=0.0.0.0 is the explicit opt-in for using the console
# from another device on a trusted LAN. It also re-exposes the API to every
# container on this machine unless deploy/host-firewall.sh is applied.
HOST="${ACTOR_PLATFORM_BIND_HOST:-127.0.0.1}"
if [[ -n "${PUBLIC_DOMAIN:-}" ]]; then
    # Never expose uvicorn directly: only Caddy, over TLS, reaches it. This
    # wins over ACTOR_PLATFORM_BIND_HOST.
    HOST="127.0.0.1"
    export ACTOR_PLATFORM_REQUIRE_HTTPS=1
fi
case "$HOST" in
    127.0.0.1|localhost|::1) ;;
    *) echo "WARNING: the API will listen on $HOST:$PORT (ACTOR_PLATFORM_BIND_HOST)." >&2
       echo "         Anything that can reach that address can reach it, including" >&2
       echo "         containers on this machine unless deploy/host-firewall.sh is applied." >&2 ;;
esac

# The Worker forwards these INTO Actor containers, by name only.
export ACTOR_FORWARD_ENV="ANTHROPIC_API_KEY"

echo "==> database"
docker compose --profile db up -d postgres >/dev/null
until docker compose --profile db exec -T postgres pg_isready -U actorplatform -q 2>/dev/null; do sleep 1; done
.venv/bin/python db/migrate.py

# The console is REBUILT WHENEVER IT IS STALE, not only when it is missing. It
# used to be built once, ever -- so after a `git pull` that changed it, this
# script restarted the API on new code while serving the OLD console, and a
# freshly promoted admin saw no Users screen with nothing to say why. Git sets a
# file's mtime when it checks it out, so "an input is newer than the build" is
# exactly "the console changed since it was last built"; an unchanged console
# costs nothing.
CONSOLE_INPUTS=(ui/console/src ui/console/index.html ui/console/package.json
                ui/console/package-lock.json ui/console/vite.config.ts ui/console/tsconfig.json)

console_stale_reason() {   # prints why the console needs building; nothing if it does not
    if [[ ! -f ui/static/index.html ]]; then
        echo "first build; this takes a minute"
    # -type f: FILES only. A directory's own mtime moves whenever anything in
    # it is created -- an editor's swap file, say -- which is not a change to
    # the console and must not force a rebuild.
    elif [[ -n "$(find "${CONSOLE_INPUTS[@]}" -type f -newer ui/static/index.html -print -quit 2>/dev/null)" ]]; then
        echo "sources changed since the last build"
    fi
}

console_needs_install() {  # dependencies missing, or the lockfile changed since the last install
    [[ ! -f ui/console/node_modules/.package-lock.json ]] ||
        [[ ui/console/package-lock.json -nt ui/console/node_modules/.package-lock.json ]]
}

reason="$(console_stale_reason)"
if [[ -n "$reason" ]]; then
    echo "==> console ($reason)"
    if console_needs_install; then (cd ui/console && npm install --silent); fi
    (cd ui/console && npm run build)
fi

pkill -f "uvicorn api.main:app" 2>/dev/null || true
pkill -f "worker/consumer.py" 2>/dev/null || true
sleep 1

echo "==> api"
setsid nohup .venv/bin/python -m uvicorn api.main:app --host "$HOST" --port "$PORT" \
    >"$LOGS/api.log" 2>&1 < /dev/null &
disown || true

echo "==> worker"
setsid nohup .venv/bin/python worker/consumer.py --actor-config config/actors.json \
    >"$LOGS/worker.log" 2>&1 < /dev/null &
disown || true

if [[ -n "${PUBLIC_DOMAIN:-}" ]]; then
    echo "==> https proxy for $PUBLIC_DOMAIN"
    UPSTREAM_PORT="$PORT" docker compose --profile public up -d caddy >/dev/null
fi

for _ in $(seq 30); do
    curl -sf --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
done

# The operator key is SHOWN ONLY AS A PREFIX: it is an admin credential, and a
# terminal's scrollback or a pasted log is not where it should live. The whole
# value is in .env.local (ACTOR_PLATFORM_API_KEYS).
OPERATOR_KEY="${ACTOR_PLATFORM_API_KEYS%%,*}"; OPERATOR_KEY="${OPERATOR_KEY%%:*}"
echo
if [[ -n "${PUBLIC_DOMAIN:-}" ]]; then
    echo "  console   https://$PUBLIC_DOMAIN/"
    echo "  operator  https://$PUBLIC_DOMAIN/ops    (key: ${OPERATOR_KEY:0:12}... -- full value in .env.local)"
else
    echo "  console   http://127.0.0.1:$PORT/"
    echo "  operator  http://127.0.0.1:$PORT/ops    (key: ${OPERATOR_KEY:0:12}... -- full value in .env.local)"
fi
echo "  listening $HOST:$PORT"
echo "  logs      $LOGS/api.log  $LOGS/worker.log"
echo
echo "Sign up with your Gmail address, then Connected accounts -> Connect facebook."
