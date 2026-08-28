# Security policy

## Reporting

Do not open a public issue containing credentials, account identifiers, device identifiers, portfolio screenshots, or position data. Use GitHub's private vulnerability reporting when enabled, or contact the maintainer privately.

## Credential handling

- Keep IBKR and ZECTRIX credentials in `.env`, a service-manager secret, or macOS Keychain.
- Restrict service environment files to the service user or root.
- Revoke and replace any credential that was committed, logged, or shared.
- Do not expose the Client Portal Gateway or an unauthenticated JSON source to the public internet.

## Scope

This application is designed for read-only portfolio retrieval and image delivery. Order placement is intentionally out of scope.
