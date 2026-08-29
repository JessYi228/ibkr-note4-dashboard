# IBKR Flex query contract

The unattended path uses the official Flex Web Service over HTTPS. Configure **XML output**. Multi-section CSV is intentionally rejected because it cannot be parsed safely as one flat table.

## Main query

Recommended period: **Last 30 Calendar Days**. The runtime also sends `p=30` by default; change `IBKR_FLEX_PERIOD_DAYS` only when the query requires another 1–365 day range.

| Section | Fields | Used for |
|---|---|---|
| Net Asset Value (NAV) Summary in Base | Report Date, Cash, Total | as-of date, cash, NAV, history |
| Open Positions | Symbol, Position, Mark Price, Position Value, FIFO PnL Unrealized, FIFO PnL Realized, Currency | position table and cumulative P&L |
| Change in NAV | From Date, To Date, Mark-to-Market, Ending Value | validation and optional one-day account P&L |

The query must return exactly one account statement. Configure one account or one consolidated statement; the runtime refuses to guess between multiple accounts.

The runtime spaces consecutive Flex `SendRequest` calls by more than one second. If IBKR returns pacing error 1018, it waits one complete minute and retries once; it never loops aggressively against the token.

## Optional daily query

Recommended period: **Last Business Day**. Set its ID as `IBKR_FLEX_DAILY_QUERY_ID`.

Add **Mark-to-Market Performance Summary in Base** with Symbol and Total. The runtime uses it for per-position day P&L and uses Change in NAV Mark-to-Market for account day P&L.

Do not substitute FIFO PnL Unrealized: it is cumulative unrealized P&L, not daily P&L.

## Availability semantics

- Missing optional values render as `N/A`, not zero.
- Buying power is not supplied by Activity Flex and remains `N/A`.
- A date-only Flex report is displayed as `AS OF MM/DD`; the fetch time is not presented as the broker data time.
- Error code 1019 is treated as pending and retried. Any final error, missing NAV, missing report date, non-XML output, or ambiguous multi-account response aborts the run before push.

## Secret-safe probing

`ibkr-note4 doctor --probe` fetches the configured source and reports only whether NAV, cash, daily P&L, and positions were recognized. It never prints values, query IDs, tokens, account IDs, or position symbols.
