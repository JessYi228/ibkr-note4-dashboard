# IBKR NOTE4 Dashboard

An unofficial, read-only bridge that renders an Interactive Brokers portfolio as a 400 × 300 monochrome dashboard and can send it to a ZECTRIX NOTE4 through the ZECTRIX Open API.

It does not use ChatGPT, Codex, an LLM, or a hosted application backend. It never places, changes, or cancels orders.

## Features

- IBKR Flex Web Service for unattended self-hosting.
- Client Portal Gateway for near-real-time sessions.
- JSON input for demos and other brokerage adapters.
- Deterministic one-bit NOTE4 rendering with NAV, P&L, positions, and a 30-day daily NAV history.
- ZECTRIX cloud delivery, local preview, Docker, and systemd timer templates.
- Secrets from environment files or macOS Keychain; identifiers are masked in diagnostics.

## Quick start

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

ibkr-note4 init
ibkr-note4 doctor
ibkr-note4 preview --output output/preview.png
```

`init` creates a mode-0600 `.env` template. Preview mode uses bundled synthetic data and needs no credentials.

## Data sources

Set `IBKR_SOURCE` in `.env`:

- `flex`: recommended for unattended VPS/NAS operation. Create an Activity Flex Query containing Net Asset Value and Open Positions, then set `IBKR_FLEX_QUERY_ID` and `IBKR_FLEX_TOKEN`.
- `client_portal`: requires a running, browser-authenticated Client Portal Gateway.
- `json`: reads a local path or HTTPS JSON endpoint. If `IBKR_JSON_SOURCE` is empty, the bundled synthetic sample is used.

The app keeps at most one locally sampled NAV per configured calendar day and retains 30 days. When a JSON provider supplies `nav_history`, that history is used directly.

## ZECTRIX setup

Set `ZECTRIX_API_KEY`. `ZECTRIX_DEVICE_ID` is optional when exactly one device is bound.

```bash
ibkr-note4 devices
ibkr-note4 run --no-push
ibkr-note4 run
```

On macOS, the existing Keychain service names are supported:

```text
ibkr-zectrix-dashboard/zectrix-api-key
ibkr-zectrix-dashboard/ibkr-flex-token
```

## Docker

```bash
cp .env.example .env
docker compose up --build
```

The container runs one fetch/render/push cycle. Use a host scheduler, Kubernetes CronJob, or the included systemd timer for recurring updates.

## Linux service

Templates are under `deploy/systemd/`. They assume installation in `/opt/ibkr-note4-dashboard`, a dedicated `ibkr-note4` user, and a root-readable `/etc/ibkr-note4-dashboard.env` file.

## Security

- Never commit `.env`, tokens, account IDs, device IDs, output images, or state files.
- Review `SECURITY.md` before exposing a JSON endpoint or running on a public VPS.
- TLS verification is enabled for Flex and ZECTRIX. Client Portal Gateway verification is configurable because its local gateway commonly uses a local certificate.

## Disclaimer

This project is unofficial and is not affiliated with or endorsed by Interactive Brokers or ZECTRIX. It is informational software, not investment advice. Verify all displayed figures against the broker before relying on them.

## License

MIT
