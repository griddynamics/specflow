"""Fixture: an application module outside the gateway package.

Real-scan fixture of `tests/test_semgrep_rules.py` (`--test` ignores `paths:`)."""

# ruleid: kit.configurable.sdk-import-outside-gateway
import vendor_sdk

_ = vendor_sdk
