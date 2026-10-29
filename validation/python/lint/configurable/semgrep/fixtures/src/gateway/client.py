"""Fixture: the gateway package, the one place allowed to import the vendor SDK.

Real-scan fixture of `tests/test_semgrep_rules.py` (`--test` ignores `paths:`)."""

# ok: kit.configurable.sdk-import-outside-gateway
import vendor_sdk

_ = vendor_sdk
