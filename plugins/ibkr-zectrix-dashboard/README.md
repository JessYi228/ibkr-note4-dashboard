# IBKR ZECTRIX Dashboard Plugin

[简体中文](README.zh-CN.md)

A public, read-only plugin for rendering an Interactive Brokers portfolio as a 400 x 300 monochrome dashboard and optionally sending a reviewed image to ZECTRIX NOTE4. It is independently developed and is not affiliated with, endorsed by, or sponsored by Interactive Brokers or ZECTRIX.

The plugin remembers non-secret choices so later runs can continue without repeating setup. Credentials remain in storage selected and controlled by each user.

## What is remembered

The preferences file contains only:

- data source;
- secret backend;
- timezone and currency;
- ZECTRIX page ID;
- maximum displayed positions.

It never stores IBKR or ZECTRIX credentials, account IDs, device IDs, passwords, or raw portfolio responses. The default location is `~/.config/ibkr-zectrix-dashboard/preferences.json`; the file is written with mode `0600` on POSIX systems.

## Secret storage choices

| Choice | Best for | Where secret values live |
| :--- | :--- | :--- |
| `keychain` | A user's Mac | macOS Keychain items owned by that user |
| `environment` | VPS, containers, CI, or cloud | Runtime environment variables injected from a protected file or secret manager |
| `auto` | A Mac that may also receive environment overrides | Environment first, then macOS Keychain |

Environment mode works with local mode-`0600` service files, GitHub Actions Secrets, Docker/Kubernetes secrets, AWS Secrets Manager or Parameter Store, Google Secret Manager, Azure Key Vault, and similar services. The plugin stores only the choice `environment`, not provider credentials or secret values.

## First setup

Do not paste API keys or tokens into chat. Ask the plugin to configure the dashboard, choose the data source and secret backend, then let it save only those choices:

The bundled renderer requires Python 3.11 or newer and Pillow 9.4 through 11.x. Check first with `python3 -c "import PIL; print(PIL.__version__)"`. If Pillow is missing, review and install the pinned range from `skills/ibkr-zectrix-dashboard/requirements.txt` only with the user's authorization.

```bash
python3 scripts/ibkr_zectrix_dashboard.py configure \
  --source codex_ibkr \
  --secret-backend keychain \
  --timezone America/New_York \
  --currency USD
```

The command prints safe next steps. In Keychain mode, the user enters each secret directly into a terminal prompt without echo. In environment mode, it lists the variable names that the user's deployment must inject.

Review remembered choices and redacted credential readiness at any time:

```bash
python3 scripts/ibkr_zectrix_dashboard.py settings --json
```

Environment variables override remembered non-secret preferences. Run `configure` again only when the user wants to change a choice.

## Data sources

- `codex_ibkr`: current data from an independently connected, read-only IBKR plugin; unavailable when that connection is not installed or authenticated.
- `flex`: unattended report-based data, generally T-1 rather than intraday.
- `client_portal`: near-real-time data while the user's local gateway session remains authenticated.
- `json`: explicit sanitized input for development or custom adapters.

## Safe validation order

```bash
python3 scripts/ibkr_zectrix_dashboard.py settings --json
python3 scripts/ibkr_zectrix_dashboard.py preview \
  --input assets/sample_snapshot.json \
  --output output/preview.png
python3 scripts/ibkr_zectrix_dashboard.py devices
python3 scripts/ibkr_zectrix_dashboard.py run --no-push
```

Review the first real image before authorizing delivery. Scheduled runs may reuse the saved preferences, but must stop rather than use sample or stale data when authentication or data retrieval fails.

See [PRIVACY.md](PRIVACY.md) for the data-handling policy. This plugin is informational software and never places, modifies, or cancels orders.

Support is available through the [public issue tracker](https://github.com/JessYi228/ibkr-note4-dashboard/issues). See [TERMS.md](TERMS.md) for the terms of use.
