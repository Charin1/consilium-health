#!/usr/bin/env bash
#
# Consilium - one-time local Tempo + Loki + Prometheus + Grafana setup.
#
#   ./scripts/setup-tempo-grafana.sh
#
# Stands up a general-purpose observability stack as a sibling of this repo:
#   OTel Collector (redacts + tail-samples traces; exports metrics)
#     -> Tempo (traces), Prometheus (metrics)
#   Grafana Alloy (tails backend.log/frontend.log) -> Loki (logs)
#   Grafana (auto-provisioned datasources for all three + cross-links
#            between them, no UI click-through)
#
# This is NOT a duplicate of Langfuse (scripts/setup-langfuse.sh), which
# traces prompts/completions/cost specifically: this covers everything else
# (HTTP, SQL, outbound calls, log lines, RED metrics), so a slow response can
# be attributed to "the database" vs "the LLM call" instead of guessed at, a
# log line can jump straight to the trace it happened inside, and a metric
# spike can jump to an example trace behind it (exemplars).
#
# Reusable: like Langfuse, this instance isn't tied to consilium-health -
# any future project can point its own OTLP exporter at the same Collector.
#
# Safe to re-run: each config file is written only if it doesn't already
# exist (hand edits are never clobbered), so running this again after a
# fresh git pull adds anything new without touching or losing what's already
# deployed. Existing Tempo/Loki/Prometheus data is never lost.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_ENV="$ROOT/backend/.env"
BACKEND_LOGS="$ROOT/logs"
TG_DIR="$(dirname "$ROOT")/tempo-grafana"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YLW=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'

say()  { printf '%s\n' "${CYN}${BOLD}tempo-grafana${RST} $*"; }
ok()   { printf '%s\n' "  ${GRN}ok${RST}    $*"; }
warn() { printf '%s\n' "  ${YLW}warn${RST}  $*"; }
die()  { printf '%s\n' "  ${RED}error${RST} $*" >&2; exit 1; }

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

write_if_missing() {  # write_if_missing <path> then heredoc via stdin
  local path="$1"
  if [[ -f "$path" ]]; then
    ok "$(basename "$path") already present - not overwriting"
  else
    cat > "$path"
    ok "wrote $(basename "$path")"
  fi
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

# --------------------------------------------------------------- port scan
# 3000/9090/9091 are deliberately not in this list - Grafana and Prometheus
# are remapped to 3002/9092 to avoid the exact collisions setup-langfuse.sh's
# web UI and MinIO already occupy on this machine.
#
# Skipped entirely if our own stack is already the thing holding these ports
# (a re-run against an existing install, e.g. to pick up a fresh git pull) -
# `docker compose up -d` below is idempotent and handles that correctly on
# its own. Only a genuine conflict (something else on these ports) should die.
if [[ -f "$TG_DIR/docker-compose.yml" ]] \
   && [[ -n "$(docker compose -f "$TG_DIR/docker-compose.yml" ps --status running -q 2>/dev/null)" ]]; then
  ok "stack already running -> $TG_DIR (re-applying config)"
else
  for wp in 4317:otel-grpc 4318:otel-http 13133:otel-health 8889:otel-prom-exporter \
            3200:tempo-api 3002:grafana 3100:loki 12345:alloy 9092:prometheus; do
    p="${wp%%:*}"; label="${wp#*:}"
    port_busy "$p" && die "host port $p ($label) is already in use - free it or edit the port map in this script"
  done
  ok "ports free (4317, 4318, 13133, 8889, 3200, 3002, 3100, 12345, 9092)"
fi

# -------------------------------------------------------------------- files
mkdir -p "$TG_DIR/grafana/provisioning/datasources"
[[ -d "$BACKEND_LOGS/backend" && -d "$BACKEND_LOGS/frontend" ]] \
  || warn "logs/backend or logs/frontend not found yet - run the app once (./start.sh) so Alloy has files to tail"

say "writing config -> $TG_DIR (only what's missing)"

write_if_missing "$TG_DIR/otel-collector.yaml" <<'EOF'
# Single ingress for app telemetry. Apps export OTLP here and know nothing
# about Tempo/Prometheus directly - swapping backends later is a
# Collector-only change.
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

  # Metrics-only batch processor, separate from the traces one above: metric
  # points arrive on their own 5s export cycle (backend/app/services/
  # telemetry.py), independent of trace volume, and tail_sampling is a
  # traces-only processor - it can't be shared into this pipeline.
  batch/metrics:
    timeout: 500ms

exporters:
  otlp/tempo:
    endpoint: tempo:4317
    tls: {insecure: true}

  # Pull-based: Prometheus scrapes this, the Collector doesn't push anywhere.
  # enable_open_metrics is what carries exemplars (a Grafana metric graph's
  # "jump to the trace behind this data point" link) - paired with
  # Prometheus's own --enable-feature=exemplar-storage (docker-compose.yml).
  prometheus:
    endpoint: 0.0.0.0:8889
    enable_open_metrics: true

extensions:
  health_check: {endpoint: 0.0.0.0:13133}

service:
  extensions: [health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resourcedetection, transform/redact, tail_sampling, batch]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, resourcedetection, batch/metrics]
      exporters: [prometheus]
  telemetry:
    logs: {level: info}
EOF

write_if_missing "$TG_DIR/tempo-config.yaml" <<'EOF'
# Tempo single-binary, local storage. No metrics_generator remote_write -
# Tempo's own span-derived RED metrics aren't used here; the app emits its
# own RED + domain metrics directly (backend/app/services/telemetry.py +
# metrics.py) straight to Prometheus via the Collector.
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

write_if_missing "$TG_DIR/loki-config.yaml" <<'EOF'
# Loki single-binary, filesystem storage. Local dev only.
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9096
  log_level: warn

common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore: {store: inmemory}

schema_config:
  configs:
    - from: 2024-04-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  # REQUIRED for trace_id to arrive as queryable structured metadata rather
  # than being dropped - that's what makes the Loki<->Tempo jump work.
  allow_structured_metadata: true
  volume_enabled: true
  ingestion_rate_mb: 16
  ingestion_burst_size_mb: 32
  reject_old_samples: true
  reject_old_samples_max_age: 168h
  # Not a PHI guarantee - the app is not designed to receive PHI in the
  # first place - but keep retention short and explicit anyway, since log
  # bodies are free text and a bug could put something sensitive in one.
  retention_period: 168h  # 7d

compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem

analytics:
  reporting_enabled: false
EOF

write_if_missing "$TG_DIR/alloy-config.alloy" <<'EOF'
// Tails consilium-health's JSON log files into Loki.
//
// Only backend.log and frontend.log are tailed, not backend_error.log -
// ERROR-level records go to BOTH backend.log and backend_error.log (two
// handlers on the same root logger, app/utils/logger.py:setup_logging), so
// tailing the error file too would duplicate every error line in Loki.

local.file_match "app_logs" {
  path_targets = [
    {__path__ = "/var/log/consilium/backend/backend.log",   service = "consilium-backend",  stream = "app"},
    {__path__ = "/var/log/consilium/frontend/frontend.log", service = "consilium-frontend", stream = "app"},
  ]
  sync_period = "5s"
}

loki.source.file "app_logs" {
  targets       = local.file_match.app_logs.targets
  forward_to    = [loki.process.otel_json.receiver]
  tail_from_end = true  // don't replay the whole (multi-MB) history on first boot
}

loki.process "otel_json" {
  // Matches OTelJsonFormatter's actual field names (app/utils/logger.py) -
  // "logger.name" has a literal dot in the key, hence the quoting.
  stage.json {
    expressions = {
      level     = "severity_text",
      logger    = "\"logger.name\"",
      trace_id  = "trace_id",
      span_id   = "span_id",
      timestamp = "timestamp",
    }
  }

  stage.timestamp {
    source = "timestamp"
    format = "RFC3339Nano"
  }

  // Bounded labels only - service (from path_targets) + level. Every label
  // value creates a separate stream; logger names and trace ids do NOT
  // belong here (unbounded cardinality), which is exactly why trace_id goes
  // to structured_metadata below instead.
  stage.labels {
    values = {level = "level"}
  }

  // trace_id as structured metadata (not a label): still queryable, still
  // what powers the Loki -> Tempo "view trace" jump in Grafana, without the
  // cardinality cost of making every distinct trace its own stream.
  stage.structured_metadata {
    values = {trace_id = "trace_id", span_id = "span_id", logger = "logger"}
  }

  forward_to = [loki.write.default.receiver]
}

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
EOF

write_if_missing "$TG_DIR/prometheus.yml" <<'EOF'
global:
  # 5s to match telemetry.py's export_interval_millis=5000 - a mismatch here
  # (e.g. Prometheus's own 15s-60s defaults) means every metric looks stale
  # by up to a scrape interval for no reason, the same latency trap already
  # hit and fixed for traces (decision_wait, batch.timeout).
  scrape_interval: 5s
  evaluation_interval: 5s
  external_labels:
    env: dev

scrape_configs:
  - job_name: otel-collector-apps
    honor_labels: true
    # Exemplar support (metric -> trace jump) comes from the server-level
    # --enable-feature=exemplar-storage flag (docker-compose.yml) plus the
    # Collector's own enable_open_metrics: true (otel-collector.yaml).
    # There's no per-scrape-config field for this in Prometheus 3.x -
    # `enable_open_metrics` here doesn't exist and just crash-loops the
    # server: "field enable_open_metrics not found in type
    # config.ScrapeConfig". Prometheus auto-negotiates OpenMetrics via the
    # Accept header once exemplar-storage is on.
    static_configs:
      - targets: ["otel-collector:8889"]

  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]   # Prometheus's own internal port, not the remapped host one

  - job_name: tempo
    static_configs:
      - targets: ["tempo:3200"]

  - job_name: loki
    static_configs:
      - targets: ["loki:3100"]
EOF

# datasources.yaml is fully generated (never hand-edited in practice), so
# it's always rewritten - the only file here without a write_if_missing
# guard. This is what lets a re-run add a new datasource + cross-links to an
# install that predates it.
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
      # Trace -> its logs. Filters Loki by trace_id structured metadata
      # (alloy-config.alloy) rather than a wide time-window guess.
      tracesToLogsV2:
        datasourceUid: loki
        spanStartTimeShift: "-2m"
        spanEndTimeShift: "2m"
        filterByTraceID: true
        filterBySpanID: false
      # Span -> the RED metrics for that operation (rate/error/duration
      # around when this specific trace ran, not the whole-service average).
      #
      # Metric name verified against the actual scrape output, not assumed:
      # this instrumentor version emits the OLDER semantic-convention name
      # http_server_duration_milliseconds (unit: ms), not the newer
      # http_server_request_duration_seconds some docs lead with. Re-check
      # with `curl localhost:8889/metrics` before trusting either name on an
      # instrumentation-library upgrade - this is exactly the kind of
      # plausible-but-wrong name that silently blanks a panel.
      tracesToMetrics:
        datasourceUid: prometheus
        spanStartTimeShift: "-5m"
        spanEndTimeShift: "5m"
        queries:
          - name: "Request rate"
            query: "sum(rate(http_server_duration_milliseconds_count{$$__tags}[5m]))"
          - name: "p95 latency (ms)"
            query: "histogram_quantile(0.95, sum by (le) (rate(http_server_duration_milliseconds_bucket{$$__tags}[5m])))"

  - name: Loki
    uid: loki
    type: loki
    access: proxy
    url: http://loki:3100
    jsonData:
      maxLines: 1000
      derivedFields:
        # Log line -> its trace. trace_id/span_id arrive as structured
        # metadata (alloy-config.alloy), which is what makes this a field
        # here rather than a regex scraped out of the JSON body.
        - name: TraceID
          matcherType: label
          matcherRegex: trace_id
          url: "${__value.raw}"
          datasourceUid: tempo
          urlDisplayLabel: "View trace"

  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    jsonData:
      httpMethod: POST
      # A graph point -> the exact trace behind that point. Requires the
      # Collector's prometheus exporter to run with enable_open_metrics: true
      # (otel-collector.yaml) - that's what actually carries the exemplar,
      # this just tells Grafana where to send you when you click one.
      exemplarTraceIdDestinations:
        - name: trace_id
          datasourceUid: tempo
          urlDisplayLabel: "View trace"
EOF
ok "wrote datasources.yaml"

write_if_missing "$TG_DIR/docker-compose.yml" <<EOF
name: tempo-grafana
# Full local observability stack: OTel Collector -> {Tempo, Loki, Prometheus}
# -> Grafana. One file, one `docker compose up -d`.

services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.123.0
    command: ["--config=/etc/otel/config.yaml"]
    volumes:
      - ./otel-collector.yaml:/etc/otel/config.yaml:ro
    ports:
      - "4317:4317"   # OTLP grpc - apps export here
      - "4318:4318"   # OTLP http
      - "13133:13133" # health_check
      - "8889:8889"   # prometheus exporter (scraped below; exposed for debug too)
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

  loki:
    image: grafana/loki:3.5.0
    command: ["-config.file=/etc/loki/config.yaml"]
    volumes:
      - ./loki-config.yaml:/etc/loki/config.yaml:ro
      - loki-data:/loki
    ports:
      - "3100:3100"
    restart: unless-stopped

  # Tails consilium-health's JSON log files into Loki. Not part of the OTel
  # Collector path -- the app writes files (otel-logging-setup skill), it
  # doesn't export logs over OTLP, so something has to read those files.
  alloy:
    image: grafana/alloy:v1.7.5
    command:
      - run
      - --server.http.listen-addr=0.0.0.0:12345
      - --storage.path=/var/lib/alloy/data
      - /etc/alloy/config.alloy
    volumes:
      - ./alloy-config.alloy:/etc/alloy/config.alloy:ro
      # consilium-health lives outside this directory (sibling project) -
      # mount its JSON log folders directly, read-only.
      - ${BACKEND_LOGS}/backend:/var/log/consilium/backend:ro
      - ${BACKEND_LOGS}/frontend:/var/log/consilium/frontend:ro
      - alloy-data:/var/lib/alloy/data
    ports:
      - "12345:12345"
    depends_on: [loki]
    restart: unless-stopped

  prometheus:
    image: prom/prometheus:v3.2.1
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=15d
      - --enable-feature=exemplar-storage   # metric point -> trace jump
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    ports:
      # remapped: Langfuse's MinIO already owns 9090 on this machine.
      - "9092:9090"
    depends_on: [otel-collector]
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
    depends_on: [tempo, loki, prometheus]
    restart: unless-stopped

volumes:
  tempo-data:
  loki-data:
  alloy-data:
  prometheus-data:
  grafana-data:
EOF

# ------------------------------------------------------------------- compose
say "starting containers (first run pulls images - a couple minutes)"
( cd "$TG_DIR" && docker compose up -d ) || die "docker compose up failed - see output above"

say "waiting for tempo + loki + prometheus + grafana to answer"
for _ in $(seq 1 40); do
  curl -fsS "http://localhost:3200/ready" >/dev/null 2>&1 \
    && curl -fsS "http://localhost:3100/ready" >/dev/null 2>&1 \
    && curl -fsS "http://localhost:9092/-/ready" >/dev/null 2>&1 \
    && curl -fsS "http://localhost:3002/api/health" >/dev/null 2>&1 \
    && break
  sleep 3
done
curl -fsS "http://localhost:3200/ready" >/dev/null 2>&1 || die "tempo did not become ready - check: docker logs tempo-grafana-tempo-1"
curl -fsS "http://localhost:3100/ready" >/dev/null 2>&1 || die "loki did not become ready - check: docker logs tempo-grafana-loki-1"
curl -fsS "http://localhost:9092/-/ready" >/dev/null 2>&1 || die "prometheus did not become ready - check: docker logs tempo-grafana-prometheus-1"
curl -fsS "http://localhost:3002/api/health" >/dev/null 2>&1 || die "grafana did not become healthy - check: docker logs tempo-grafana-grafana-1"
ok "tempo + loki + prometheus + grafana healthy"

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

# Same endpoint carries traces and metrics - one Collector, one pipeline
# ingress. Nothing metrics-specific to add here; setup_telemetry() (telemetry.py)
# turns on a MeterProvider automatically once OTEL_EXPORTER_OTLP_ENDPOINT is set.
set_or_append OTEL_SERVICE_NAME "consilium-backend"
set_or_append OTEL_EXPORTER_OTLP_ENDPOINT "http://localhost:4317"
set_or_append OTEL_TRACES_SAMPLER "parentbased_traceidratio"
set_or_append OTEL_TRACES_SAMPLER_ARG "1.0"
ok "backend/.env updated"

printf '\n'
say "ready"
printf '  %sGrafana%s     http://localhost:3002  (admin / admin)\n' "$BOLD" "$RST"
printf '  %sTempo%s       http://localhost:3200\n' "$BOLD" "$RST"
printf '  %sLoki%s        http://localhost:3100\n' "$BOLD" "$RST"
printf '  %sPrometheus%s  http://localhost:9092\n' "$BOLD" "$RST"
printf '  %sstop%s        cd %s && docker compose stop\n' "$BOLD" "$RST" "$TG_DIR"
printf '\n'
printf '  Just start the app as usual (./start.sh) - tracing, metrics, and log shipping are live already.\n'
