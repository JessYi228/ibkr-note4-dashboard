# IBKR NOTE4 Dashboard

An unofficial, read-only bridge that fetches Interactive Brokers portfolio data, renders a deterministic 400 × 300 monochrome PNG, and sends it to a ZECTRIX NOTE4 through the ZECTRIX Open API.

The runtime is standalone: it does not use ChatGPT, Codex, an LLM, or a hosted application backend. It never places, changes, or cancels orders.

## What it supports

- IBKR Flex Web Service for unattended VPS, NAS, cron, container, and cloud-job use.
- Client Portal Gateway for interactive, near-real-time desktop sessions.
- Explicit JSON input for tests and third-party read-only adapters.
- One-bit NOTE4 rendering with NAV, optional P&L, positions, cash, and up to 30 daily NAV points.
- Fail-closed Flex parsing: errors, pending reports, missing NAV, and missing report dates are never rendered as zero-value dashboards.
- Transient ZECTRIX retry, unchanged-display deduplication, and optional Healthchecks-compatible monitoring.
- Docker, hardened systemd templates, CI, and an opt-in GitHub Actions refresh workflow.

NOTE4 black-and-white is the supported target. NOTE4C four-color output is not implemented yet.

## Quick start

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

ibkr-note4 init
ibkr-note4 preview --output output/preview.png
ibkr-note4 doctor
```

`preview` is the only command that defaults to the bundled synthetic sample. A live `run` never falls back to sample data.

## Choose a data source

Set `IBKR_SOURCE` in the mode-0600 `.env` file:

- `flex` — recommended for unattended operation. It needs only HTTPS access, a Flex token, and one or two query IDs.
- `client_portal` — near-real-time, but requires a running Client Portal Gateway and interactive browser authentication.
- `json` — reads an explicitly configured local path or HTTPS endpoint. `IBKR_JSON_SOURCE` is mandatory in live run mode.

### Recommended Flex setup

Create a main Activity Flex Query using XML output and a 30-day period. Include:

- **Net Asset Value (NAV) Summary in Base:** Report Date, Cash, Total.
- **Open Positions:** Symbol, Position, Mark Price, Position Value, FIFO PnL Unrealized, FIFO PnL Realized, Currency.
- **Change in NAV:** From Date, To Date, Mark-to-Market, Ending Value.

Set its ID as `IBKR_FLEX_QUERY_ID`. The main query supplies the current snapshot and stateless 30-day NAV history.

For true `DAY P&L`, create an optional second XML query with period **Last Business Day** and include:

- Change in NAV: From Date, To Date, Mark-to-Market, Ending Value.
- Mark-to-Market Performance Summary in Base: Symbol and Total.
- The same NAV Summary and Open Positions fields as the main query.

Set that ID as `IBKR_FLEX_DAILY_QUERY_ID`. Without this daily query, day P&L is rendered as `N/A`; cumulative FIFO unrealized P&L is never mislabeled as daily P&L. Buying power is also `N/A` in Flex mode because it is not an Activity Flex field.

See [docs/flex-query.md](docs/flex-query.md) for the complete contract and troubleshooting guidance.

## Validate before pushing

```bash
ibkr-note4 doctor
ibkr-note4 doctor --probe
ibkr-note4 run --no-push
ibkr-note4 devices
ibkr-note4 run
```

`doctor --probe` reports only recognized-field availability, never financial values. Inspect the generated 400 × 300 image before the first live push.

A successful push stores only a display-data SHA-256 and timestamp in `state/last-push.json`. Future unchanged snapshots are skipped; use `run --force` only when an unchanged image must be resent.

## Secrets

Never commit `.env`, Flex tokens, API keys, account IDs, device IDs, output images, or state files.

On a VPS, use a root-readable service environment file such as `/etc/ibkr-note4-dashboard.env` with mode `0600`. On macOS, these Keychain service names are supported:

```text
ibkr-zectrix-dashboard/zectrix-api-key
ibkr-zectrix-dashboard/ibkr-flex-token
```

## Docker

```bash
cp .env.example .env
chmod 600 .env
mkdir -p output state
# Linux bind mounts must be writable by the image's uid 10001:
sudo chown 10001:10001 output state
docker compose run --rm dashboard ibkr-note4 run --no-push
docker compose run --rm dashboard
```

The image installs DejaVu Sans explicitly; it will not silently render with Pillow's tiny fallback bitmap font.

## systemd

Templates under `deploy/systemd/` assume installation at `/opt/ibkr-note4-dashboard`, a dedicated `ibkr-note4` user, a mode-0600 `/etc/ibkr-note4-dashboard.env`, and service-owned `output/` and `state/` directories.

The supplied timer runs once on weekdays at `10:17 UTC`. Activity Flex data is generally T-1, so hourly polling is not the default. Holiday runs normally deduplicate and skip an unchanged push.

## GitHub Actions without a VPS

`.github/workflows/refresh.yml` is manual-only by default. Add the documented repository secrets, run it once with `no_push=true`, then opt into a cron schedule in your fork. It does not upload portfolio images or use Actions cache as a database; the 30-day Flex query supplies history directly.

Scheduled Actions can be delayed or disabled on inactive public repositories, so a small VPS or NAS remains the more dependable unattended option.

## Monitoring

Set `HEALTHCHECK_URL` to a private Healthchecks-compatible ping URL. Successful and deduplicated runs ping the URL; failed runs make a best-effort request to `/fail`. The URL itself is a secret and must not be committed.

## Security and disclaimer

- Review [SECURITY.md](SECURITY.md) before exposing a JSON endpoint or Client Portal Gateway.
- TLS verification is always enabled for Flex and ZECTRIX. Client Portal TLS verification is configurable only for its local gateway certificate.
- The project is unofficial and is not affiliated with or endorsed by Interactive Brokers or ZECTRIX.
- Displayed figures are informational. Verify them against the broker before relying on them.

## License

MIT
