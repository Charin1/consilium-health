#!/usr/bin/env bash
#
# Consilium - start backend and frontend together.
#
#   ./start.sh            start both
#   ./start.sh backend    backend only
#   ./start.sh frontend   frontend only
#   ./start.sh check      verify the environment, start nothing
#
# Ctrl-C stops everything it started.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
LOGS="$ROOT/logs"
VENV="$BACKEND/.venv"
PY="$VENV/bin/python"

BACKEND_PORT="${PORT:-8000}"
FRONTEND_PORT=5173

# Match the interpreter the project is developed against. Falls back through
# what is actually installed rather than assuming `python3` is new enough -
# the loader needs 3.9+, and 3.13 is what CI and the venv here use.
PY_CANDIDATES=(
  "$HOME/.local/share/uv/python/cpython-3.13.6-macos-aarch64-none/bin/python3.13"
  "$(command -v python3.13 || true)"
  "$(command -v python3.12 || true)"
  "$(command -v python3.11 || true)"
  "$(command -v python3 || true)"
)

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YLW=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'

say()  { printf '%s\n' "${CYN}${BOLD}consilium${RST} $*"; }
ok()   { printf '%s\n' "  ${GRN}ok${RST}    $*"; }
warn() { printf '%s\n' "  ${YLW}warn${RST}  $*"; }
die()  { printf '%s\n' "  ${RED}error${RST} $*" >&2; exit 1; }

PIDS=()
cleanup() {
  printf '\n'
  say "shutting down"
  for pid in "${PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
    fi
  done
  ok "stopped"
}
trap cleanup EXIT INT TERM

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# ---------------------------------------------------------------- backend env
ensure_backend() {
  if [[ ! -x "$PY" ]]; then
    local base=""
    for cand in "${PY_CANDIDATES[@]}"; do
      [[ -n "$cand" && -x "$cand" ]] && { base="$cand"; break; }
    done
    [[ -n "$base" ]] || die "no python3 found on PATH"

    local ver
    ver="$("$base" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    say "creating venv with python $ver"
    "$base" -m venv "$VENV" || die "could not create venv at $VENV"
    "$PY" -m pip install --quiet --upgrade pip
  fi

  # Reinstall only when requirements.txt is newer than the last install stamp.
  local stamp="$VENV/.requirements-stamp"
  if [[ ! -f "$stamp" || "$BACKEND/requirements.txt" -nt "$stamp" ]]; then
    say "installing backend dependencies"
    "$PY" -m pip install --quiet -r "$BACKEND/requirements.txt" \
      || die "pip install failed"
    touch "$stamp"
    ok "dependencies installed"
  else
    ok "dependencies up to date ($("$PY" -V 2>&1))"
  fi

  if [[ ! -f "$BACKEND/.env" ]]; then
    cp "$BACKEND/.env.example" "$BACKEND/.env"
    warn ".env created from .env.example - add your model API key before debating"
    warn "  $BACKEND/.env"
  else
    # Loud about a half-configured state rather than failing at first LLM call.
    if grep -qE '^GROQ_API_KEY=your_groq_api_key_here' "$BACKEND/.env" \
       && grep -qE '^LLM_PROVIDER=groq' "$BACKEND/.env"; then
      warn "LLM_PROVIDER=groq but GROQ_API_KEY is still the placeholder"
      warn "  advisor turns will fail until you set it, or switch to LLM_PROVIDER=demo"
    else
      ok ".env configured"
    fi
  fi
}

ensure_frontend() {
  [[ -d "$FRONTEND/node_modules" ]] && { ok "node_modules present"; return; }
  command -v npm >/dev/null 2>&1 || die "npm not found - install Node.js"
  say "installing frontend dependencies"
  ( cd "$FRONTEND" && npm install --silent ) || die "npm install failed"
  ok "frontend dependencies installed"
}

# ------------------------------------------------------------------- roster
show_roster() {
  PYTHONPATH="$BACKEND" "$PY" - <<'PY' 2>/dev/null || warn "could not load persona roster"
from app.services.persona_loader import available_packs, load_personas, load_pack_manifest
packs = available_packs()
total = len(load_personas(packs))
print(f"  \033[32mok\033[0m    roster: {total} seats across {len(packs)} packs")
for p in packs:
    m = load_pack_manifest(p)
    seats = load_personas(p)
    own = [s for s in seats if not s.get("inherited_from")]
    print(f"        \033[2m{m['display_name']:<26} {len(own):>2} own / {len(seats):>2} total\033[0m")
PY
}

# -------------------------------------------------------------------- launch
start_backend() {
  if port_busy "$BACKEND_PORT"; then
    die "port $BACKEND_PORT already in use - stop the other process or set PORT=..."
  fi
  mkdir -p "$LOGS"
  say "starting backend on :$BACKEND_PORT"
  ( cd "$BACKEND" && PYTHONPATH=. "$PY" -m uvicorn main:app \
      --reload --host 0.0.0.0 --port "$BACKEND_PORT" \
      > "$LOGS/backend.log" 2>&1 ) &
  PIDS+=($!)

  # Wait for it to actually answer, rather than claiming success immediately.
  for _ in $(seq 1 40); do
    if curl -fsS "http://localhost:$BACKEND_PORT/health" >/dev/null 2>&1; then
      ok "backend healthy    http://localhost:$BACKEND_PORT/docs"
      return 0
    fi
    sleep 0.5
  done
  warn "backend did not answer /health within 20s - see $LOGS/backend.log"
  tail -n 15 "$LOGS/backend.log" 2>/dev/null | sed 's/^/        /'
}

start_frontend() {
  if port_busy "$FRONTEND_PORT"; then
    die "port $FRONTEND_PORT already in use"
  fi
  mkdir -p "$LOGS"
  say "starting frontend on :$FRONTEND_PORT"
  ( cd "$FRONTEND" && npm run dev > "$LOGS/frontend.log" 2>&1 ) &
  PIDS+=($!)
  sleep 2
  ok "frontend            http://localhost:$FRONTEND_PORT"
}

# ---------------------------------------------------------------------- main
MODE="${1:-all}"

case "$MODE" in
  check)
    say "checking environment"
    ensure_backend
    ensure_frontend
    show_roster
    say "environment ready - run ./start.sh to launch"
    trap - EXIT; exit 0
    ;;
  backend)
    ensure_backend; show_roster; start_backend
    ;;
  frontend)
    ensure_frontend; start_frontend
    ;;
  all)
    ensure_backend
    ensure_frontend
    show_roster
    start_backend
    start_frontend
    ;;
  *)
    die "unknown mode '$MODE' - use: all | backend | frontend | check"
    ;;
esac

printf '\n'
say "running - ${DIM}logs in $LOGS/, Ctrl-C to stop${RST}"
wait
