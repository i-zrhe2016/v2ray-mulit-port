# Single-Server Multi-Port V2Fly Panel Design

## Summary

This project expands the current single-port V2Fly deployment into a single-server multi-port management panel. Each managed port represents exactly one user subscription and has its own UUID, traffic quota, expiration time, V2Ray subscription link, and Clash subscription link.

The implementation keeps `v2fly/v2fly-core` as the proxy core. Runtime port registration and removal use the official V2Fly remote control API instead of rebuilding the container for each change. Persistent state is stored in a single JSON file that acts as the source of truth for panel state and runtime reconciliation.

## Confirmed Scope

### In Scope

- Single server only
- Multi-port management panel
- One port equals one user subscription
- Manual port assignment
- Total traffic quota per port
- Automatic disable when traffic quota is exhausted
- Automatic disable when expiration time is reached
- Manual add, delete, enable, disable, quota update, expiration update, and traffic reset operations
- Directly usable V2Ray and Clash subscription links per port
- Persistent state stored in JSON
- HTML management page served by the backend

### Out of Scope

- Multi-server or multi-node orchestration
- Account/password login for the panel
- Recurring monthly quotas
- Billing, payment, or order management
- Reseller or end-user self-service portal
- Protocols other than VMess over WebSocket in the first version

## Existing Project Constraints

- The repository currently exposes a single VMess + WebSocket port behind an Nginx gateway.
- The existing API returns JSON wrappers for VMess and Clash links instead of direct subscription content.
- The current deployment does not manage users, quotas, expirations, or runtime port state.
- The project must continue to use `v2fly/v2fly-core`, not Xray.

These constraints drive the design toward a Python control plane that owns persistent state and synchronizes that state into a long-running V2Fly process through the official remote control API.

## Architecture

The system is split into four focused units.

### 1. Panel Service

A Python service provides both the HTML admin page and JSON management API. It owns all mutation logic:

- validate add or update requests
- write changes to the JSON state file
- reconcile desired state into V2Fly runtime
- generate subscription links
- expose current port status for the admin page

### 2. JSON State Store

A single JSON file persists all configured ports and their latest known runtime state. This file is the source of truth. The panel never treats in-memory state or V2Fly runtime state as authoritative over JSON.

### 3. V2Fly Runtime Sync

The V2Fly container enables the official remote control API, including:

- `HandlerService` for adding and removing inbounds at runtime
- `StatsService` for reading cumulative traffic counters

The panel uses these services to keep the running process aligned with the desired state stored in JSON.

### 4. Background Reconciler

A periodic background loop:

- reads cumulative traffic stats for every managed port
- updates `traffic_used_bytes`
- recalculates status
- disables expired or exhausted ports
- repairs runtime drift by reapplying the desired state

## Runtime Model

The design keeps one long-running `v2fly/v2fly-core` instance. Each managed user is represented by one inbound:

- `port` is unique per user
- `uuid` is unique per user
- `ws_path` is unique per user
- `tag` is derived from the port and used for runtime operations
- `email` is derived from the port and used for V2Fly traffic stats

The panel does not restart the V2Fly container for routine add or delete operations. Instead, it calls `HandlerService` to add or remove the corresponding inbound. This avoids full service restarts and keeps changes near real time.

On panel startup, a full reconciliation pass reconstructs runtime state from the JSON file so that the system recovers cleanly after `docker compose down/up` or host restart.

## Data Model

Persistent state is stored in a JSON file such as `data/ports.json`.

Example shape:

```json
{
  "version": 1,
  "server": {
    "public_host": "example.com",
    "public_scheme": "https",
    "subscription_base_url": "https://example.com",
    "subconverter_url": "https://example.com/sub"
  },
  "ports": [
    {
      "port": 20001,
      "uuid": "2f6f9c15-0db2-4ef7-a7d5-4ec9d6d3c3c7",
      "remark": "user-20001",
      "ws_path": "/ws/20001",
      "alter_id": 0,
      "enabled": true,
      "status": "active",
      "traffic_limit_bytes": 107374182400,
      "traffic_used_bytes": 123456789,
      "traffic_reset_base_bytes": 0,
      "expires_at": "2026-05-01T00:00:00Z",
      "created_at": "2026-04-10T00:00:00Z",
      "updated_at": "2026-04-10T00:00:00Z",
      "last_synced_at": "2026-04-10T00:00:00Z",
      "last_sync_error": "",
      "subscription_token": "random-token"
    }
  ]
}
```

### Record Fields

- `port`: the externally reachable VMess port, manually assigned and unique
- `uuid`: the VMess client UUID for this port
- `remark`: display name shown in the panel and used in subscription metadata
- `ws_path`: the WebSocket path for this port, unique to avoid one shared path for all users
- `alter_id`: fixed at `0` for current VMess compatibility
- `enabled`: manual operator switch
- `status`: derived runtime-facing state; allowed values are `active`, `disabled`, `expired`, `exhausted`, and `sync_error`
- `traffic_limit_bytes`: total allowed quota
- `traffic_used_bytes`: logical displayed usage after subtracting any manual reset baseline
- `traffic_reset_base_bytes`: cumulative byte counter snapshot used to implement logical traffic reset without restarting V2Fly
- `expires_at`: RFC 3339 UTC timestamp at which the port becomes unavailable
- `created_at` and `updated_at`: audit timestamps
- `last_synced_at`: timestamp of the last successful runtime sync
- `last_sync_error`: latest sync error message, if any
- `subscription_token`: random opaque identifier for subscription URLs

### State Rules

`status` is derived from `enabled`, `expires_at`, `traffic_used_bytes`, and the most recent sync result:

- `disabled` when `enabled` is `false`
- `expired` when current time is after `expires_at`
- `exhausted` when `traffic_used_bytes >= traffic_limit_bytes`
- `sync_error` when runtime reconciliation fails for that port
- `active` otherwise

The reconciler recalculates `status` on every sync cycle to prevent drift between stored state and actual rules.

## Subscription Model

Each port exposes two directly usable subscription endpoints:

- `/subscriptions/{token}/v2ray`
- `/subscriptions/{token}/clash`

### V2Ray Subscription

The V2Ray endpoint returns a directly importable subscription payload for that single port. The payload contains a standard VMess link encoded as a subscription response instead of wrapping it inside management JSON.

### Clash Subscription

The Clash endpoint uses the port's V2Ray subscription URL as the source and forwards it through `subconverter` to produce a Clash-compatible subscription result. If `subconverter` is unavailable, the panel reports a subscription generation failure without changing the runtime status of the port itself.

### Subscription Security Boundary

The panel has no login in this version, because it is intended for internal deployment only. Subscription URLs therefore rely on opaque `subscription_token` values instead of exposing subscription access by raw port number alone. This is not a replacement for proper public security, but it avoids trivial sequential enumeration.

## Admin UI and API

The Python panel serves both the HTML page and JSON API.

### HTML Page

- `GET /admin`

The page shows:

- port
- remark
- current status
- used and total traffic
- expiration time
- last sync result
- V2Ray subscription link
- Clash subscription link

The page provides forms or buttons for:

- add port
- delete port
- enable or disable port
- update remark
- update total quota
- update expiration time
- reset traffic usage
- trigger manual sync

### JSON API

- `GET /api/ports`
- `POST /api/ports`
- `PATCH /api/ports/{port}`
- `POST /api/ports/{port}/reset-traffic`
- `POST /api/ports/{port}/sync`
- `DELETE /api/ports/{port}`
- `GET /links/{token}`

Expected behaviors:

- `POST /api/ports` validates uniqueness of `port`, `uuid`, `ws_path`, and `subscription_token`
- `PATCH /api/ports/{port}` allows updating `remark`, `enabled`, `traffic_limit_bytes`, and `expires_at`
- `POST /api/ports/{port}/reset-traffic` sets `traffic_used_bytes` to `0` in JSON and resets the runtime counter baseline used by the panel
- `DELETE /api/ports/{port}` removes the port from JSON and removes the runtime inbound
- `POST /api/ports/{port}/sync` performs immediate reconciliation for one port
- `GET /links/{token}` returns the directly usable V2Ray and Clash subscription URLs for panel copy actions

## Runtime Synchronization

### Startup Recovery

When the panel starts:

1. load and validate the JSON state file
2. connect to the V2Fly remote control API
3. reconcile every stored port against runtime state
4. ensure only ports with derived `active` status remain registered in V2Fly
5. update `last_synced_at`, `last_sync_error`, and `status`

This makes the JSON file sufficient for recovery after restarts.

### Periodic Sync

The background loop runs at a fixed interval and performs:

1. read cumulative uplink and downlink counters from `StatsService`
2. compute cumulative total bytes per port
3. compute `traffic_used_bytes` as `max(0, cumulative_total_bytes - traffic_reset_base_bytes)`
4. recompute status
5. add missing active inbounds through `HandlerService`
6. remove non-active inbounds through `HandlerService`
7. persist the updated JSON file atomically

### Resetting Traffic

V2Fly stats are cumulative from process start. To support manual traffic reset without restarting the proxy, the panel will treat reset as a logical reset:

- the JSON record stores `traffic_reset_base_bytes` as the baseline offset
- displayed `traffic_used_bytes` is computed as current cumulative bytes minus the saved baseline
- `POST /api/ports/{port}/reset-traffic` updates `traffic_reset_base_bytes` to the current cumulative total so displayed usage returns to zero

## Deployment Changes

The current Nginx single-port gateway layout is not sufficient for per-user dynamic external ports. The deployment must change in these ways:

- the proxy core must be able to listen on multiple externally reachable ports
- the panel service must be reachable for `/admin`, `/api/*`, and subscription endpoints
- the remote control API must remain internal to the Docker network
- the JSON state file must be mounted on persistent storage

The recommended deployment shape is:

- `v2ray` container for the proxy core
- `panel` container for HTML, API, reconciliation, and subscription generation
- `subconverter` container for Clash conversion
- optional lightweight reverse proxy for the panel endpoints only

The concrete Docker strategy is:

- publish a fixed allowed user port range from the host to the `v2ray` container, for example `20000-29999:20000-29999`
- validate all manually entered ports against that configured range
- keep the V2Fly remote control API on an internal-only container port that is not published to the host
- expose the panel endpoints through a dedicated admin HTTP port or a small reverse proxy, separate from user proxy ports

This keeps Docker networking simple while still allowing runtime creation and removal of individual ports inside a bounded range.

## Validation Rules

The panel rejects requests when:

- the port is already present in JSON
- the port is outside the configured allowed range
- the UUID already exists
- the WebSocket path already exists
- the quota is not a positive integer
- the expiration timestamp is invalid
- the requested port conflicts with a reserved internal service port

The panel generates defaults for fields the operator does not provide:

- generate UUID when omitted
- default `remark` to `user-{port}`
- default `ws_path` to `/ws/{port}`
- default `subscription_token` to a secure random string

## Error Handling

### Corrupt JSON State

If the JSON file cannot be parsed or fails schema validation, the panel must refuse to start and surface a clear error. Running with a partially understood state file risks deleting or misclassifying ports.

### Runtime Sync Failure

If a V2Fly runtime operation fails for one port:

- preserve the JSON record
- record the failure in `last_sync_error`
- set `status` to `sync_error`
- continue syncing unrelated ports

### Stats Read Failure

If stats cannot be read during one cycle:

- keep the last known `traffic_used_bytes`
- record the sync failure timestamp and message
- do not reset or guess traffic
- retry on the next cycle

### Clash Conversion Failure

If `subconverter` is unavailable:

- leave the port's runtime state unchanged
- report Clash subscription generation failure separately
- continue serving the V2Ray subscription endpoint

## Testing Strategy

The implementation must follow test-first development. Tests should cover four layers.

### 1. State Store Tests

- load valid JSON
- reject invalid JSON
- detect duplicate ports
- derive status correctly for active, disabled, expired, exhausted, and sync error cases
- persist updates atomically

### 2. Subscription Tests

- generate a valid VMess payload for one port
- generate a valid V2Ray subscription response for one port
- generate the expected Clash converter URL for one port
- reject unknown or deleted subscription tokens

### 3. Admin API Tests

- add a port successfully
- reject duplicate port
- update quota
- update expiration
- disable and re-enable a port
- reset traffic usage
- delete a port

### 4. Runtime Sync Tests

- register active ports with `HandlerService`
- remove disabled, expired, and exhausted ports
- update traffic usage from `StatsService`
- mark one port as `sync_error` while continuing to sync the rest
- restore runtime state from JSON after process restart

## Implementation Notes

- Keep implementation boundaries explicit: state store, V2Fly client, subscription builder, reconciler, and HTTP handlers should be separate modules.
- Use atomic writes for JSON persistence to avoid partially written state files.
- Keep the remote control API off the public interface.
- Preserve the existing minimal deployment behavior only where it does not conflict with dynamic multi-port requirements.

## Acceptance Criteria

The design is complete when the implemented system can:

- create a new port from the panel and make it usable without rebuilding containers
- generate direct V2Ray and Clash subscription links for that port
- show used versus total traffic for each port
- persist data across `docker compose down/up` and host restart
- disable a port automatically when it expires
- disable a port automatically when its total traffic is exhausted
- delete a port cleanly from both persistent state and runtime
- recover runtime state from the JSON file on service startup
