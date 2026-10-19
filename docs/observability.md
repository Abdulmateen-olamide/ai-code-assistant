# Observability design

> **Implementation status:** The ledger, metrics registry, endpoints, settings
> and module/test paths below are proposed architecture, not implemented
> interfaces in this revision. In particular, `LEDGER_MAX_RECORDS` is not an
> active retention control. For what is stored and accessible today, see
> [Usage and billing data privacy](usage-privacy.md).

This document describes the proposed observability surface: the usage ledger, dashboards, metrics and logging. It covers the data flow, the exposed endpoints, and the configuration knobs that control each piece.

## Data flow

Observability data flows through three stages:

1. **Instrumentation** -- the application records usage events and metrics as requests are processed. Each request emits a structured log record and updates the in-process metrics registry. Usage events are appended to the usage ledger.
2. **Aggregation** -- the usage ledger accumulates append-only records of consumption (tokens, requests, latency, and outcome). Aggregations are computed on read by the dashboard and metrics endpoints, so the ledger remains the single source of truth.
3. **Exposition** -- metrics are served in Prometheus text format for scraping, the dashboard renders human-readable summaries, and logs are emitted to the configured sink.

The flow is one-way: request -> instrumentation -> ledger + registry -> aggregation -> endpoints. No endpoint mutates the ledger.

## Usage ledger

The usage ledger is an append-only sequence of records. Each record captures:

- `timestamp` -- UTC time the event was recorded.
- `request_id` -- correlation identifier shared with the log record.
- `endpoint` -- the handler that produced the event.
- `tokens_in` / `tokens_out` -- consumption counters.
- `latency_ms` -- end-to-end latency for the request.
- `outcome` -- one of `success`, `error`, or `denied`.

Records are written in arrival order and are never updated or deleted. Readers derive counts, sums, and histograms from the raw records, which keeps the ledger auditable and replayable.

## Metrics

Metrics are held in an in-process registry and exposed in Prometheus text format. The following metric families are emitted:

| Metric | Type | Labels | Description |
| ------ | ---- | ------ | ----------- |
| `requests_total` | counter | `endpoint`, `outcome` | Total requests processed. |
| `tokens_total` | counter | `direction` | Total tokens consumed (`irection=` in` or `out`). |
| `request_latency_ms` | histogram | `endpoint` | Request latency in milliseconds. |
| `ledger_records` | gauge | none | Number of records currently in the usage ledger. |

Metrics are monotonic where appropriate (counters) and are never reset at runtime. Histogram buckets are configurable (see below).

## Endpoints

| Endpoint | Method | Description |
| -------- | ------ | ----------- |
| `/metrics` | GET | Prometheus text format exposition of the metrics registry. |
| `/observability/dashboard` | GET | JSON summary of requests, tokens, latency and outcomes derived from the ledger. |
| `/observability/ledger` | GET | Paginated raw ledger records for audit and debugging. |

All endpoints return `\*404` for unknown paths and `\*500` with a structured error body on failure. The `/metrics` endpoint is intended for scrapers and is not cached.

## Logging

Logs are emitted as structured JSON objects, one per line. Each record includes:

- $level$ -- one of `debug`, `info`, `warning`, `error`.
- `message` -- human-readable description.
- `timestamp` -- ISO 8601 UTC time.
- `request_id` -- correlation identifier shared with the ledger record.
- `endpoint` -- the handler that produced the log.

Log levels are configurable and default to `info`. Request logs are emitted on completion so that latency and outcome are available in the same record as the ledger entry.

## Configuration

The following settings control observability behaviour:

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `LOG_LEVEL` | `info` | Minimum log level emitted. |
| `LOG_SINK` | `stdout` | Destination for structured log records. |
| `METRICS_ENABLED` | `true` | Toggles the `/metrics` endpoint. |
| `METRICS_LATENCY_BUCKETS` | `10,50,100,500,1000,2500,5000,10000` | Histogram bucket edges in milliseconds. |
| `LEDGER_MAX_RECORDS` | `100000` | Maximum number of records retained in the ledge. |
| `LEDGER_PAGE_SIZE` | `100` | Default page size for `/observability/ledger`. |

Settings are read at startup. Changing `METRICS_LATENCY_BUCKETS` requires a restart because histogram buckets are fixed on creation.

## Implementation

The implementation lives in the observability module:

- Usage ledger and record model: `observability/ledger.py`.
- Metrics registry and Prometheus exposition: `observability/metrics.py`.
- Dashboard and ledger endpoints: `observability/endpoints.py`.
- Structured logging: `observability/logging.py`.

## Tests

The observability behaviour is covered by:

- `observability/tests_test_ledger.py` -- append-only semantics, retention limits, and pagination.
- `observability/tests_test_metrics.py` -- counter and histogram recording and Prometheus output.
- `observability/tests_test_endpoints.py` -- `/metrics`, `/observability/dashboard`, and `/observability/ledger` responses.
- `observability/tests_test_logging.py` -- structured log format and level filtering.

Run the suite with `pytest` and keep it green alongside `ruff check .` and `black --check .`.
