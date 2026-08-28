# Changelog

## 0.2.0 - 2026-08-28

- Made live execution independent of Codex and disabled all sample-data fallback outside `preview`.
- Replaced permissive Flex parsing with fail-closed XML validation, real report dates, cash/NAV history mapping, pending/error handling, and single-statement enforcement.
- Added an optional Last Business Day query for semantically correct account and position daily P&L.
- Preserved unavailable financial fields as `N/A` instead of misleading zero values.
- Added dynamic timezone labels, date-only `AS OF` display, deterministic small e-paper digits, and a collecting-history state.
- Added transient ZECTRIX retry, display-data deduplication, atomic secret-safe state, and optional healthcheck pings.
- Added an explicit Docker font dependency, container/systemd hardening, T-1 weekday scheduling, and an opt-in GitHub Actions refresh workflow.
- Expanded synthetic tests for Flex errors, mapping, daily P&L semantics, retry, timezone handling, missing values, HTTPS enforcement, and push deduplication.
