# Contributing

1. Use synthetic fixtures only.
2. Do not add order-placement capabilities.
3. Keep credentials and identifiers out of tests, screenshots, logs, and commits.
4. Run `python -m unittest discover -s tests -v` before opening a pull request.
5. Include a 400 × 300 preview for renderer changes, using the bundled sample data.
6. Use sanitized, synthetic XML shaped like a real Flex response; never commit downloaded account statements.
7. Treat unavailable financial fields as `None`/`N/A`. Do not coerce missing broker data to zero or substitute a cumulative P&L field for daily P&L.
8. Live integration tests must remain opt-in and must never push to a real device from CI.
