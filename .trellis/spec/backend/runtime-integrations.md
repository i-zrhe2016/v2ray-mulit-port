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

---

## Scenario: Unified Server Settings

### 1. Scope / Trigger

This applies when changing panel-level V2Ray or traffic integration settings. The panel stores these settings centrally under `state["server"]`, exposes them through the API and admin page, and uses them for runtime sync, subscription generation, and traffic reads.

### 2. Signatures

HTTP API:

```http
GET /api/settings
PATCH /api/settings
```

Service methods:

```python
def get_settings() -> dict[str, Any]: ...
def update_settings(payload: dict[str, Any]) -> dict[str, Any]: ...
```

State shape:

```json
{
  "server": {
    "v2ray_api_server": "127.0.0.1:10085",
    "public_v2ray_host": "example.com",
    "public_tls": false,
    "traffic_stats_source": "v2ray",
    "nginx_stats_json_path": "/data/nginx-traffic.json"
  }
}
```

### 3. Contracts

Fields:

| Field | Type | Contract |
| --- | --- | --- |
| `v2ray_api_server` | string | Non-blank address passed to `v2ray api --server=...` for runtime operations. |
| `public_v2ray_host` | string | Optional host written to generated VMess payloads. Blank means use request-derived fallback. |
| `public_tls` | boolean | Controls whether generated VMess payloads include `tls`. |
| `traffic_stats_source` | string | Canonical values are `v2ray` and `nginx_json`; aliases may be accepted but must persist canonical values. |
| `nginx_stats_json_path` | string | Container-readable stats file path used when source is `nginx_json`. |

Environment defaults:

| Env key | State field |
| --- | --- |
| `V2RAY_API_SERVER` | `v2ray_api_server` |
| `V2RAY_PUBLIC_HOST` | `public_v2ray_host` |
| `V2RAY_PUBLIC_TLS` | `public_tls` |
| `TRAFFIC_STATS_SOURCE` | `traffic_stats_source` |
| `NGINX_TRAFFIC_STATS_FILE` | `nginx_stats_json_path` |

Environment values are startup defaults only for fresh state or missing fields. Once settings are saved, state wins until the field is removed from the state file.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Unknown PATCH field | Return HTTP 400 / raise `ValueError`; do not persist partial changes. |
| Blank `v2ray_api_server` | Return HTTP 400 / raise `ValueError`. |
| Unsupported `traffic_stats_source` | Return HTTP 400 / raise `ValueError`. |
| `traffic_stats_source=nginx_json` with blank `nginx_stats_json_path` | Return HTTP 400 / raise `ValueError`. |
| Settings saved successfully | Persist under `state["server"]`, reconfigure runtime/traffic clients, and run subsequent syncs with saved values. |
| Runtime server address changes | Clear in-memory applied-tag tracking so active ports are applied to the new runtime target on sync. |

### 5. Good/Base/Bad Cases

Good: An operator saves `traffic_stats_source=nginx_json` and `nginx_stats_json_path=/data/nginx-traffic.json`; the next sync reads that file without rebuilding the container.

Base: A fresh state file has no `server` object fields. The panel seeds the missing settings from environment variables and persists normalized values.

Bad: Code reads `V2RAY_PUBLIC_HOST` directly during subscription generation after settings have been saved. This makes saved UI settings ineffective and violates the state-wins contract.

### 6. Tests Required

Tests for unified settings must assert:

* fresh/missing settings are populated from defaults and persisted under `state["server"]`;
* `GET /api/settings` returns the current settings;
* `PATCH /api/settings` validates unknown fields, unsupported sources, blank V2Ray API server, and blank NGINX path in NGINX mode;
* saving `v2ray_api_server` updates the runtime client's server address before later sync/add/remove calls;
* saving `public_v2ray_host` and `public_tls` changes generated VMess payloads;
* saving `traffic_stats_source` and `nginx_stats_json_path` changes the traffic client used by later sync/reset reads.

### 7. Wrong vs Correct

#### Wrong

```python
public_host = os.getenv("V2RAY_PUBLIC_HOST", "")
traffic_client = build_traffic_client(runtime_client)
```

This keeps environment variables as the live source of truth and ignores saved panel settings.

#### Correct

```python
settings = panel_service.get_settings()
runtime_client.server = settings["v2ray_api_server"]
traffic_client = build_traffic_client(runtime_client, settings)
```

Runtime integration code must read the normalized state-backed settings after startup defaults have been applied.
