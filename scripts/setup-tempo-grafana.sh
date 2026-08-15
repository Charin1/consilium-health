#!/usr/bin/env bash
#
# Consilium - one-time local Tempo + Grafana setup.
#
#   ./scripts/setup-tempo-grafana.sh
#
# Stands up a general-purpose tracing stack as a sibling of this repo:
# OTel Collector (redacts + tail-samples) -> Tempo (stores traces) ->
# Grafana (auto-provisioned with a Tempo datasource, no UI click-through).
#
# This is deliberately traces-only - no Prometheus, no Loki. It answers "was
# this slow request the database or the LLM call", which general app spans
# (HTTP, SQL, outbound calls) already cover; it is NOT a duplicate of
# Langfuse, which traces prompts/completions/cost specifically
# (see scripts/setup-langfuse.sh). Both write to the same OTel trace_id
# format so a trace found in one system can be pasted into the other's search.
#
# Reusable: like Langfuse, this instance isn't tied to consilium-health -
# any future project can point its own OTLP exporter at the same Collector.
#
# Safe to re-run: won't regenerate configs or lose Tempo's stored traces on
# an instance that already exists - it just makes sure everything is up and
# backend/.env is current.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_ENV="$ROOT/backend/.env"
TG_DIR="$(dirname "$ROOT")/tempo-grafana"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YLW=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'

say()  { printf '%s\n' "${CYN}${BOLD}tempo-grafana${RST} $*"; }
ok()   { printf '%s\n' "  ${GRN}ok${RST}    $*"; }
warn() { printf '%s\n' "  ${YLW}warn${RST}  $*"; }
die()  { printf '%s\n' "  ${RED}error${RST} $*" >&2; exit 1; }

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

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

# --------------------------------------------------------------- port scan
# 3000 is deliberately not in this list - Grafana here is mapped to 3002 to
# avoid the exact collision setup-langfuse.sh's web UI already occupies.
for wp in 4317:otel-grpc 4318:otel-http 13133:otel-health 3200:tempo-api 3002:grafana; do
  p="${wp%%:*}"; label="${wp#*:}"
  port_busy "$p" && die "host port $p ($label) is already in use - free it or edit the port map in this script"
done
ok "ports free (4317, 4318, 13133, 3200, 3002)"

# -------------------------------------------------------------------- files
mkdir -p "$TG_DIR/grafana/provisioning/datasources"

if [[ -f "$TG_DIR/docker-compose.yml" ]]; then
  ok "config already present -> $TG_DIR (not overwriting - edit by hand if you need changes)"
else
  say "writing config -> $TG_DIR"

  cat > "$TG_DIR/otel-collector.yaml" <<'EOF'
# Single ingress for app telemetry. Apps export OTLP here and know nothing
# about Tempo directly - swapping backends later is a Collector-only change.
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 20

  resourcedetection:
    detectors: [env, system]
    system:
      hostname_sources: [os]

  # PHI / PII redaction - deny by default. This stack traces generic app
  # spans (HTTP, SQL, outbound calls), not prompts/completions - those go
  # through Langfuse's own masking (scripts/setup-langfuse.sh) - but request
  # bodies and SQL parameter values still need to be stripped here.
  transform/redact:
    error_mode: ignore
    trace_statements:
      - context: span
        statements:
          - delete_key(attributes, "db.statement")
          - delete_key(attributes, "http.request.body")
          - delete_key(attributes, "http.response.body")
          - replace_pattern(attributes["url.full"], "\\?.*$", "")
          - replace_pattern(attributes["http.url"], "\\?.*$", "")

  # Keep every error and every slow trace; sample the boring ones.
  tail_sampling:
    # 10s (a typical production value, giving retries/stragglers time to
    # land before the keep/drop decision) adds pure latency on a local
    # single-developer stack, stacked on top of the app's own batch delay
    # and this collector's own batch processor below -- combined they made
    # a trace take 20-40s to become searchable at all. 2s is enough to
    # still catch same-request retries without that wait feeling broken.
    decision_wait: 2s
    num_traces: 50000
    policies:
      - name: errors
        type: status_code
        status_code: {status_codes: [ERROR]}
      - name: slow
        type: latency
        latency: {threshold_ms: 2000}
      - name: baseline
        type: probabilistic
        # 100% for local dev - a single-developer stack should show you the
        # trace you just sent, not drop it 80% of the time. Lower this for a
        # shared/production deployment (errors and slow requests are always
        # kept regardless, via the policies above).
        probabilistic: {sampling_percentage: 100}

  batch:
    timeout: 500ms
    send_batch_size: 1024
    send_batch_max_size: 2048

exporters:
  otlp/tempo:
    endpoint: tempo:4317
    tls: {insecure: true}

extensions:
  health_check: {endpoint: 0.0.0.0:13133}

service:
  extensions: [health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resourcedetection, transform/redact, tail_sampling, batch]
      exporters: [otlp/tempo]
  telemetry:
    logs: {level: info}
EOF

  cat > "$TG_DIR/tempo-config.yaml" <<'EOF'
# Tempo single-binary, local storage. No metrics_generator remote_write -
# there is no Prometheus in this stack to receive it; leaving that pointed
# at a nonexistent target would just fill the log with failed writes.
server:
  http_listen_port: 3200
  log_level: warn

# Serve the streaming API over HTTP too. Streaming is disabled on the Grafana
# datasource side (see datasources.yaml), so this is belt-and-braces rather
# than load-bearing - but it means enabling streaming later won't re-break
# TraceQL search.
stream_over_http_enabled: true

distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318

ingester:
  max_block_duration: 5m

compactor:
  compaction:
    block_retention: 168h   # 7d

storage:
  trace:
    backend: local
    wal:
      path: /var/tempo/wal
    local:
      path: /var/tempo/blocks

usage_report:
  reporting_enabled: false
EOF

  cat > "$TG_DIR/grafana/provisioning/datasources/datasources.yaml" <<'EOF'
apiVersion: 1

datasources:
  - name: Tempo
    uid: tempo
    type: tempo
    access: proxy
    url: http://tempo:3200
    isDefault: true
    jsonData:
      # Streaming OFF deliberately. Grafana's Tempo plugin does streaming
      # search over gRPC, but this datasource URL is Tempo's HTTP port
      # (3200), so every TraceQL search ({} etc.) failed with:
      #   rpc error: code = Unavailable desc = connection error:
      #   desc = "error reading server preface: http2: frame too large"
      # Trace-ID lookup kept working throughout because that path is plain
      # HTTP GET /api/traces/{id}, not the gRPC streaming search - which is
      # exactly why "search by ID works but {} shows nothing" was the symptom.
      streamingEnabled:
        search: false
      search: {hide: false}
      traceQuery:
        timeShiftEnabled: true
        spanStartTimeShift: "-5m"
        spanEndTimeShift: "5m"
EOF

  cat > "$TG_DIR/docker-compose.yml" <<'EOF'
name: tempo-grafana

services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.123.0
    command: ["--config=/etc/otel/config.yaml"]
    volumes:
      - ./otel-collector.yaml:/etc/otel/config.yaml:ro
    ports:
      - "4317:4317"
      - "4318:4318"
      - "13133:13133"
    depends_on: [tempo]
    restart: unless-stopped

  tempo:
    image: grafana/tempo:2.7.0
    command: ["-config.file=/etc/tempo/config.yaml"]
    volumes:
      - ./tempo-config.yaml:/etc/tempo/config.yaml:ro
      - tempo-data:/var/tempo
    ports:
      - "3200:3200"
    restart: unless-stopped

  grafana:
    image: grafana/grafana:11.6.0
    environment:
      GF_SECURITY_ADMIN_USER: admin
      GF_SECURITY_ADMIN_PASSWORD: admin
      GF_AUTH_ANONYMOUS_ENABLED: "false"
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - grafana-data:/var/lib/grafana
    ports:
      # remapped: Langfuse's web UI already owns 3000 on this machine.
      - "3002:3000"
    depends_on: [tempo]
    restart: unless-stopped

volumes:
  tempo-data:
  grafana-data:
EOF

  ok "wrote config"
fi

# ------------------------------------------------------------------- compose
say "starting containers (first run pulls images - a couple minutes)"
( cd "$TG_DIR" && docker compose up -d ) || die "docker compose up failed - see output above"

say "waiting for tempo + grafana to answer"
for _ in $(seq 1 40); do
  curl -fsS "http://localhost:3200/ready" >/dev/null 2>&1 \
    && curl -fsS "http://localhost:3002/api/health" >/dev/null 2>&1 \
    && break
  sleep 3
done
curl -fsS "http://localhost:3200/ready" >/dev/null 2>&1 || die "tempo did not become ready - check: docker logs tempo-grafana-tempo-1"
curl -fsS "http://localhost:3002/api/health" >/dev/null 2>&1 || die "grafana did not become healthy - check: docker logs tempo-grafana-grafana-1"
ok "tempo + grafana healthy"

# --------------------------------------------------------------- backend/.env
[[ -f "$BACKEND_ENV" ]] || { warn "backend/.env missing - copying from .env.example first"; cp "$ROOT/backend/.env.example" "$BACKEND_ENV"; }

set_or_append() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$BACKEND_ENV"; then
    sed -i.bak "s#^${key}=.*#${key}=${value}#" "$BACKEND_ENV" && rm -f "$BACKEND_ENV.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$BACKEND_ENV"
  fi
}

set_or_append OTEL_SERVICE_NAME "consilium-backend"
set_or_append OTEL_EXPORTER_OTLP_ENDPOINT "http://localhost:4317"
set_or_append OTEL_TRACES_SAMPLER "parentbased_traceidratio"
set_or_append OTEL_TRACES_SAMPLER_ARG "1.0"
ok "backend/.env updated"

printf '\n'
say "ready"
printf '  %sGrafana%s  http://localhost:3002  (admin / admin)\n' "$BOLD" "$RST"
printf '  %sTempo%s    http://localhost:3200\n' "$BOLD" "$RST"
printf '  %sstop%s     cd %s && docker compose stop\n' "$BOLD" "$RST" "$TG_DIR"
printf '\n'
printf '  Just start the app as usual (./start.sh) - tracing is live already.\n'
