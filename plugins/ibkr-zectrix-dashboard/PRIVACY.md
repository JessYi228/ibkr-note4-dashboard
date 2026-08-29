# Privacy Policy

Last updated: August 29, 2026

IBKR ZECTRIX Dashboard is a local, read-only plugin that renders portfolio data for a ZECTRIX NOTE4 display.

## Data processed

Depending on the data source selected by the user, the plugin may process portfolio totals, position symbols, quantities, prices, profit and loss, and NAV history. Processing occurs in the user's local Codex environment or in infrastructure selected and controlled by the user.

## Credentials and identifiers

The plugin does not ask users to send credentials through chat and does not store secrets in its preferences file. Flex tokens and ZECTRIX API keys are read at runtime from the user's macOS Keychain or environment variables supplied by a protected local file or secret-management service. Account IDs, device IDs, tokens, API keys, and raw IBKR responses must not be committed to source control or included in logs.

## Stored data

The optional preferences file contains only non-secret choices such as data source, secret backend, timezone, currency, page ID, and maximum displayed positions. It is written with private file permissions where supported. Dedupe state contains only a SHA-256 fingerprint and push timestamp. Rendered dashboard images and any optional local history remain in storage controlled by the user.

## Network requests

When enabled by the user, the plugin may contact Interactive Brokers data services and the ZECTRIX Open API. The plugin does not operate a developer-controlled analytics or data-collection backend.

## Retention and deletion

The developer does not receive or retain user portfolio data. Users control local and deployment storage and may delete the preferences, output, state, and history files at any time. Deleting Keychain items or cloud/local secrets is handled through the storage provider selected by the user.

## Contact

Report privacy or security issues through the repository's GitHub issue tracker without including credentials, identifiers, or portfolio data.
