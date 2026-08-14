#!/usr/bin/env bash
#
# Consilium - one-time local Langfuse setup.
#
#   ./scripts/setup-langfuse.sh
#
# Clones langfuse/langfuse as a sibling of this repo, brings it up via its own
# docker-compose.yml (Postgres + ClickHouse + Redis + MinIO + web + worker),
# auto-provisions an org/project/user/API-keys on first boot (no clicking
# through the UI), and writes the resulting LANGFUSE_* vars into
# backend/.env.
#
# Safe to re-run: it will not re-clone, re-generate secrets, or re-provision
# an instance that already exists - it just makes sure everything is up and
# backend/.env is current.
#
# What it does NOT do: touch anything in this repo's own git history, expose
# any port beyond 127.0.0.1/localhost, or send anything to Langfuse's cloud -
# LANGFUSE_HOST is always the local instance this script starts.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_ENV="$ROOT/backend/.env"
LF_DIR="$(dirname "$ROOT")/langfuse"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YLW=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'

say()  { printf '%s\n' "${CYN}${BOLD}langfuse${RST} $*"; }
ok()   { printf '%s\n' "  ${GRN}ok${RST}    $*"; }
warn() { printf '%s\n' "  ${YLW}warn${RST}  $*"; }
die()  { printf '%s\n' "  ${RED}error${RST} $*" >&2; exit 1; }

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

next_free_port() {
  local p="$1"
  while port_busy "$p"; do p=$((p + 1)); done
  echo "$p"
}

# ------------------------------------------------------------------- docker
command -v docker >/dev/null 2>&1 || die "docker not found - install Docker Desktop first"

if ! docker info >/dev/null 2>&1; then
  if [[ "$(uname)" == "Darwin" ]]; then
    say "starting Docker Desktop"
    open -a Docker 2>/dev/null || die "could not launch Docker Desktop - start it manually"
    for _ in $(seq 1 40); do
      docker info >/dev/null 2>&1 && break
      sleep 2
    done
  fi
  docker info >/dev/null 2>&1 || die "docker daemon is not running"
fi
ok "docker running"

# --------------------------------------------------------------------- clone
if [[ -d "$LF_DIR/.git" ]]; then
  ok "langfuse already cloned -> $LF_DIR"
else
  say "cloning langfuse/langfuse -> $LF_DIR"
  git clone --depth 1 https://github.com/langfuse/langfuse.git "$LF_DIR" \
    || die "clone failed"
  ok "cloned"
fi

# ---------------------------------------------------------------- .env + keys
LF_ENV="$LF_DIR/.env"

if [[ -f "$LF_ENV" ]]; then
  ok ".env already present - not regenerating (would break the existing project's login)"
else
  say "generating local secrets"
  # POSTGRES_PASSWORD and MINIO_ROOT_PASSWORD are referenced by name in more
  # than one place in langfuse's own docker-compose.yml (DATABASE_URL and the
  # S3 upload access keys respectively default to *different* literal values
  # than these). Generate once here and build every dependent var FROM that
  # value, rather than trusting the compose file's own defaults to match -
  # they don't, and a mismatch fails silently until the web container
  # crash-loops on an auth error.
  PG_PW="$(openssl rand -hex 16)"
  MINIO_PW="$(openssl rand -hex 16)"

  PG_PORT="$(next_free_port 5432)"
  [[ "$PG_PORT" != "5432" ]] && warn "host port 5432 busy - Langfuse's Postgres will use :$PG_PORT instead (container-internal traffic is unaffected)"

  cat > "$LF_ENV" <<EOF
NEXTAUTH_SECRET=$(openssl rand -hex 32)
SALT=$(openssl rand -hex 16)
ENCRYPTION_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=${PG_PW}
CLICKHOUSE_PASSWORD=$(openssl rand -hex 16)
MINIO_ROOT_PASSWORD=${MINIO_PW}
REDIS_AUTH=$(openssl rand -hex 16)

# Must match POSTGRES_PASSWORD above - the compose file's own DATABASE_URL
# default does not.
DATABASE_URL=postgresql://postgres:${PG_PW}@postgres:5432/postgres

# Must match MINIO_ROOT_PASSWORD above - same trap as DATABASE_URL, on the S3
# side. All three (event/media/batch-export) share one MinIO user.
LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY=${MINIO_PW}
LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY=${MINIO_PW}
LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY=${MINIO_PW}

# Auto-provisioned on first boot - no UI click-through needed.
LANGFUSE_INIT_ORG_ID=consilium
LANGFUSE_INIT_ORG_NAME=Consilium
LANGFUSE_INIT_PROJECT_ID=consilium-health
LANGFUSE_INIT_PROJECT_NAME=Consilium Health
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(python3 -c 'import uuid; print(uuid.uuid4())')
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(python3 -c 'import uuid; print(uuid.uuid4())')
LANGFUSE_INIT_USER_EMAIL=${LANGFUSE_ADMIN_EMAIL:-admin@localhost}
LANGFUSE_INIT_USER_NAME=Admin
LANGFUSE_INIT_USER_PASSWORD=$(openssl rand -hex 12)
EOF
  ok "wrote $LF_ENV"

  if [[ "$PG_PORT" != "5432" ]]; then
    # Only the host-side port changes; postgres:5432 (Docker-internal DNS) is
    # what every other container actually talks to, so DATABASE_URL above is
    # correct as written regardless of this remap.
    sed -i.bak "s/- 127.0.0.1:5432:5432/- 127.0.0.1:${PG_PORT}:5432/" "$LF_DIR/docker-compose.yml" \
      && rm -f "$LF_DIR/docker-compose.yml.bak"
  fi
fi

# ------------------------------------------------------------------ port scan
# These are safe to leave running on a busy port and just warn about: unlike
# postgres/minio-secrets above, remapping them correctly requires touching
# more than one dependent var (e.g. MinIO's externally-facing endpoint URLs),
# which this script has not verified end-to-end. Free the port or edit
# $LF_DIR/docker-compose.yml by hand if one of these is taken.
for wp in 3000:web-ui 6379:redis 8123:clickhouse-http 9000:clickhouse-native 9090:minio 9091:minio-console; do
  p="${wp%%:*}"; label="${wp#*:}"
  port_busy "$p" && warn "host port $p ($label) is already in use - see comment above if 'docker compose up' fails"
done

# ------------------------------------------------------------------- compose
say "starting containers (first run pulls images - a few minutes)"
( cd "$LF_DIR" && docker compose up -d ) || die "docker compose up failed - see output above"

# langfuse-web's host port is left at its default (3000) by this script -
# the port scan above only warns if it's taken, it doesn't remap it.
HOST_PORT=3000
say "waiting for langfuse-web to answer"
for _ in $(seq 1 40); do
  curl -fsS "http://localhost:${HOST_PORT}/api/public/health" >/dev/null 2>&1 && break
  sleep 3
done
curl -fsS "http://localhost:${HOST_PORT}/api/public/health" >/dev/null 2>&1 \
  || die "langfuse-web did not become healthy - check: docker logs langfuse-langfuse-web-1"
ok "langfuse-web healthy -> http://localhost:${HOST_PORT}"

# --------------------------------------------------------------- backend/.env
PUB_KEY="$(grep '^LANGFUSE_INIT_PROJECT_PUBLIC_KEY=' "$LF_ENV" | cut -d= -f2)"
SEC_KEY="$(grep '^LANGFUSE_INIT_PROJECT_SECRET_KEY=' "$LF_ENV" | cut -d= -f2)"

[[ -f "$BACKEND_ENV" ]] || { warn "backend/.env missing - copying from .env.example first"; cp "$ROOT/backend/.env.example" "$BACKEND_ENV"; }

set_or_append() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$BACKEND_ENV"; then
    sed -i.bak "s#^${key}=.*#${key}=${value}#" "$BACKEND_ENV" && rm -f "$BACKEND_ENV.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$BACKEND_ENV"
  fi
}

set_or_append LANGFUSE_PUBLIC_KEY "$PUB_KEY"
set_or_append LANGFUSE_SECRET_KEY "$SEC_KEY"
set_or_append LANGFUSE_HOST "http://localhost:${HOST_PORT}"
set_or_append LANGFUSE_SAMPLE_RATE "1.0"
ok "backend/.env updated"

ADMIN_EMAIL="$(grep '^LANGFUSE_INIT_USER_EMAIL=' "$LF_ENV" | cut -d= -f2)"
ADMIN_PW="$(grep '^LANGFUSE_INIT_USER_PASSWORD=' "$LF_ENV" | cut -d= -f2)"

printf '\n'
say "ready"
printf '  %sUI%s      http://localhost:%s\n' "$BOLD" "$RST" "$HOST_PORT"
printf '  %slogin%s   %s / %s\n' "$BOLD" "$RST" "$ADMIN_EMAIL" "$ADMIN_PW"
printf '  %sstop%s    cd %s && docker compose stop\n' "$BOLD" "$RST" "$LF_DIR"
printf '\n'
printf '  Just start the app as usual (./start.sh) - tracing is live already.\n'
