# Rules for agents working in this repository

- Read-only IBKR use only. Never add order placement, modification or cancellation, and never add trading-capable credentials or APIs.
- The runtime must not depend on any LLM, Codex, Paseo or agent harness.
- Never read, print, log or commit the user's settings file (`~/.config/ibkr-zectrix-dashboard/env`), tokens, account IDs, device IDs or real portfolio values. Use the fictional sample and test fixtures.
- Do not push to the real device or call IBKR with real credentials unless the user explicitly asks in the current conversation.
- Never fabricate freshness: failures must stay visible (STALE banner), never fall back to sample or older data silently.
- Use `uv sync --frozen --python /usr/bin/python3` and `uv run --frozen pytest`.
