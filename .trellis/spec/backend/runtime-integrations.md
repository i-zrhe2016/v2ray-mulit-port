# Runtime Integrations

> Executable contracts for backend integrations with proxy/runtime services.

---

## Scenario: Traffic Statistics Source

### 1. Scope / Trigger

This applies when changing how the panel reads per-port traffic usage. Traffic accounting is an infrastructure integration because it is driven by environment variables and external runtime files or APIs, and it directly controls quota exhaustion.

### 2. Signatures

Backend traffic clients must expose:

```python
def get_port_totals(records: Iterable[dict]) -> dict[int, int]:
    ...
```

Returned values are cumulative total bytes keyed by managed port number. `PanelService` computes displayed usage as:

```python
traffic_used_bytes = max(0, cumulative_total - traffic_reset_base_bytes)
```

### 3. Contracts

Environment keys:

| Key | Values | Contract |
| --- | --- | --- |
| `TRAFFIC_STATS_SOURCE` | `v2ray`, `v2ray_stats`, `v2ray-stats`, `nginx`, `nginx_json`, `nginx-json` | Selects the production traffic client. Empty or unset means `v2ray`. |
| `NGINX_TRAFFIC_STATS_FILE` | absolute or container-readable path | Required when the selected source is NGINX JSON. Defaults to `/data/nginx-traffic.json`. |

NGINX JSON format:

```json
{
  "20001": 123456789,
  "20002": 987654321
}
```

The JSON object must include every currently managed port. Values must be non-negative integer cumulative byte totals. Numeric strings are accepted; booleans, floats, empty strings, negative values, missing keys, arrays, and nested objects are invalid.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Unsupported `TRAFFIC_STATS_SOURCE` | Raise `ValueError` during server configuration. |
| NGINX mode with blank `NGINX_TRAFFIC_STATS_FILE` | Raise `ValueError` during server configuration. |
| Stats file missing/unreadable/invalid JSON | Raise `RuntimeSyncError`. |
| Stats root is not a JSON object | Raise `RuntimeSyncError`. |
| Managed port is missing from the JSON file | Raise `RuntimeSyncError`. |
| Port total is not a non-negative integer | Raise `RuntimeSyncError`. |
| One sync cycle cannot read totals | Preserve last known `traffic_used_bytes`, set `last_sync_error`, and keep the port in `sync_error` when rule status is otherwise active. |
| Manual traffic reset cannot read a current cumulative total | Fail the reset with `RuntimeSyncError`; do not guess a zero baseline. |

### 5. Good/Base/Bad Cases

Good: NGINX writes a complete cumulative JSON object into the mounted data directory. The panel reads all managed ports and quota exhaustion uses the computed logical usage.

Base: `TRAFFIC_STATS_SOURCE` is unset. The panel uses V2Ray StatsService as the traffic client for backward compatibility.

Bad: The NGINX file only contains ports that recently had traffic. Missing managed ports must be treated as sync errors, not as zero usage.

### 6. Tests Required

Tests for traffic source changes must assert:

* source selection defaults to the runtime client;
* NGINX aliases select the JSON traffic client and preserve the configured path;
* valid NGINX JSON returns cumulative integer totals by port;
* missing file, invalid JSON, missing managed port, and invalid total values raise `RuntimeSyncError`;
* sync errors preserve previous usage and surface `last_sync_error`;
* reset records the current cumulative total as `traffic_reset_base_bytes`;
* quota exhaustion uses logical usage after subtracting the reset baseline.

### 7. Wrong vs Correct

#### Wrong

```python
totals = traffic_client.get_port_totals(records)
record["traffic_used_bytes"] = totals.get(port, 0)
```

This silently turns missing external stats into zero usage.

#### Correct

```python
totals = traffic_client.get_port_totals(records)
if port in totals:
    record["traffic_used_bytes"] = max(0, totals[port] - record["traffic_reset_base_bytes"])
```

The traffic client must raise when a managed port is missing, and the service must preserve stored usage when a stats read fails.
