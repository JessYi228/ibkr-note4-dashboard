# Security policy

## Reporting

Do not open a public issue containing credentials, account identifiers, device identifiers, portfolio screenshots, or position data. Use GitHub's private vulnerability reporting when enabled, or contact the maintainer privately.

## Credential handling

- Keep IBKR and ZECTRIX credentials in `.env`, a service-manager secret, or macOS Keychain.
- Treat `HEALTHCHECK_URL` as a secret because possession of the URL can alter monitor state.
- Restrict service environment files to the service user or root.
- Revoke and replace any credential that was committed, logged, or shared.
- Do not expose the Client Portal Gateway or an unauthenticated JSON source to the public internet.
- Do not upload rendered portfolio images or raw Flex responses as public CI artifacts.
- GitHub Actions users should keep account IDs out of workflow variables; only query IDs, tokens, API keys, and optional device IDs belong in encrypted repository secrets.

## Scope

This application is designed for read-only portfolio retrieval and image delivery. Order placement is intentionally out of scope.

Live data validation is fail-closed: a pending or failed Flex report, ambiguous multi-account response, missing NAV, missing report date, or non-XML response must stop before image delivery.
