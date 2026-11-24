# Phase 1 public-only source guard

The Phase 1 CI safety test scans executable Python syntax under src/baqs and
rejects representative private Binance USD-M endpoints, user-data-stream
references, API-key header literals, API-key/secret/signature identifiers,
HMAC imports, and Binance SDK imports.

The guard deliberately ignores comments and module/class/function docstrings.
It includes positive tests for the public market-data routes used by this
project: exchange time and metadata, klines, funding rates, premium index,
depth, recent trades, and aggregate trades.

## Scope and limitations

This is a narrow regression guard for the Phase 1 public-only boundary. It is
not a complete Python security analyzer, a proof that arbitrary dynamic code
cannot construct a private request, a dependency audit, or a runtime network
firewall. It may not detect obfuscated strings, dynamically assembled URLs,
reflection, native extensions, or behavior hidden inside third-party packages.
It must be complemented by code review, dependency review, CI, and deployment
network policy. Phase 1 must not contain exchange credentials, authenticated
clients, account access, user-data streams, order placement, or leverage changes.
