"""Exception types whose messages are safe to log and to draw on the display."""


class DashboardError(RuntimeError):
    """Actionable error. Messages never contain tokens, account IDs or URLs with secrets."""


class FlexRateLimitError(DashboardError):
    """IBKR Flex pacing response (error 1018)."""
