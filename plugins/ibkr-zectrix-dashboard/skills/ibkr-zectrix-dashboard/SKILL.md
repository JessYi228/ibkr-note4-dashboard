---
name: ibkr-zectrix-dashboard
description: Configure, preview, refresh, schedule, validate, or deploy a read-only IBKR portfolio dashboard for a ZECTRIX NOTE4 400x300 e-paper display. Use for IBKR positions, portfolio trend, net liquidation, P&L, ZECTRIX delivery, recurring refreshes, remembered settings, or secret storage guidance.
---

# IBKR ZECTRIX Dashboard

Use the `scripts/ibkr_zectrix_dashboard.py` bundled inside this skill. Resolve paths from the directory containing this `SKILL.md`; never assume a user-specific absolute path or depend on files outside this skill bundle.

Before the first preview in an interactive session, verify that Pillow is importable. If it is missing, read [references/runtime.md](references/runtime.md), report the missing dependency, and obtain authorization before installing it. Never install dependencies during an unattended refresh.

## Non-negotiable safety boundaries

- Treat IBKR as read-only. Never place, modify, or cancel orders.
- Never ask a user to paste a Flex token, ZECTRIX API key, account ID, device ID, password, or raw portfolio response into chat.
- Never print or persist those values in logs, screenshots, source files, preferences, state files, or Git.
- The preferences file stores only non-secret choices and must remain mode `0600`. Environment variables override remembered choices.
- Live delivery requires a separate persisted `delivery_authorized` approval; it contains no secret or identifier.
- Store secrets only in the user's macOS Keychain or inject them at runtime from a protected local environment file or cloud secret manager.
- Preview the 400 x 300 image before the first live push. Do not silently widen permissions or change the selected device.
- In relay or automation mode, never use sample or stale data after any read, authentication, rendering, or delivery failure.

## Reproducible sample preview

The sample path requires no accounts, credentials, network access, or physical device. Use it for installation checks and reviewer testing:

```bash
python3 scripts/ibkr_zectrix_dashboard.py preview \
  --input assets/sample_snapshot.json \
  --output output/sample-preview.png
```

Confirm that the result is a 400 x 300 1-bit PNG before attempting any configured data source.

## First-run configuration

1. Run `settings --json`. This reports only remembered non-secret choices and whether credentials are present; it never prints credential values.
2. If preferences are absent or incomplete, ask the user to choose:
   - data source: `codex_ibkr`, `flex`, `client_portal`, or `json`;
   - secret backend: `keychain` on macOS, `environment` for a local protected environment or cloud secret manager, or `auto` for environment-first with Keychain fallback;
   - optional timezone, currency, page ID, and maximum displayed positions.
3. Do not ask for the secret values. Save only the choices with, for example:

```bash
python3 scripts/ibkr_zectrix_dashboard.py configure \
  --source codex_ibkr \
  --secret-backend keychain \
  --timezone America/New_York \
  --currency USD \
  --page-id 1 \
  --max-positions 4
```

4. Follow the command's secret-storage instructions:
   - Keychain commands prompt in the user's own terminal without putting the value in the command or shell history.
   - Environment mode lists required variable names. The user or deployment platform must inject them from a protected file, GitHub Actions Secrets, Docker/Kubernetes secrets, or a cloud secret manager.
5. Run `settings --json` again. If a required credential is absent, stop and tell the user exactly which variable or Keychain item is missing without requesting its value in chat.

On later runs, read and honor the saved preferences. Do not ask the same setup questions again unless the preferences are missing, invalid, incompatible with the current platform, or the user asks to change them.

## Data sources

- `codex_ibkr`: use only when a separately installed and authenticated read-only Interactive Brokers plugin exposes all four calls listed below. It is optional and is not bundled with this skill.
- `flex`: unattended report-based VPS/NAS data, generally T-1 rather than intraday.
- `client_portal`: near-real-time data while the local gateway is authenticated; periodic browser reauthentication is required.
- `json`: explicit sanitized input for development or a custom adapter.

## Connected IBKR relay

Before every connected IBKR read sequence, run `python3 scripts/ibkr_zectrix_dashboard.py authorize --check`. If it fails, run `python3 scripts/ibkr_zectrix_dashboard.py authorize` in an interactive terminal and wait for the user's answer before calling IBKR. The command asks whether to allow the four approved read-only IBKR calls and delivery of the sanitized dashboard to ZECTRIX, then stores only the boolean approval in the owner-readable preferences file. If declined or absent, stop before account reads; every push path also fails closed. Revoke later with `python3 scripts/ibkr_zectrix_dashboard.py authorize --revoke`.

First confirm that the separately installed IBKR plugin exposes every call below. If any call is unavailable, stop and offer the credential-free sample preview or another explicitly configured data source; do not invent a tool or claim that the dependency is bundled.

Use only these read calls when they are available:

- `get_account_summary`
- `get_account_positions`
- `get_account_balances`
- `get_pa_performance_all_periods`

Discard account-map keys, included-account lists, account IDs, contract IDs, order IDs, and every field not needed by the display. Create one compact object whose `source` is exactly `codex_ibkr`; include only display currency, totals, sanitized positions, and the one-month NAV series when available.

Pass the compact object directly through standard input. Never save or print the raw response or compact object:

```bash
python3 scripts/ibkr_zectrix_dashboard.py relay \
  --input - \
  --output output/ibkr-live.png
```

After the user reviews the first real preview, an authorized refresh may deduplicate and push atomically:

```bash
python3 scripts/ibkr_zectrix_dashboard.py relay \
  --input - \
  --output output/ibkr-live.png \
  --push \
  --dedupe-state state/last-push.json
```

The dedupe state contains only a display-data SHA-256 and push timestamp. Use `--force` only when the user explicitly requests resending an unchanged image.

## Recurring refreshes

For an authorized recurring refresh, use a thread heartbeat when connected IBKR tools are required. Restate the read-only calls, in-memory sanitization, no-file relay, no-sample-fallback rule, output/state paths, and deduplicated push command in the automation prompt.

- Run one real preview and one live push before activating a schedule.
- Treat the enabled schedule as standing authorization for one deduplicated image push per run.
- Notify on failures; an unchanged dedupe skip is success.
- Never create or migrate credentials, unlock Keychain, change device selection, or widen permissions during an automated run.
- Stop without pushing when the selected data source or credential backend is unavailable.

## Safe commands

Run from the plugin root:

```bash
python3 scripts/ibkr_zectrix_dashboard.py settings --json
python3 scripts/ibkr_zectrix_dashboard.py preview --input assets/sample_snapshot.json --output output/preview.png
python3 scripts/ibkr_zectrix_dashboard.py devices
python3 scripts/ibkr_zectrix_dashboard.py run --no-push
```

Only use `run` without `--no-push`, `relay --push`, or `push` after the first real preview has been reviewed and delivery is authorized.
