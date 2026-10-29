"""Fixtures for banned_terms.yml. The docstring spells none of the configured terms."""

# ruleid: kit.configurable.no-banned-term
BRAND = "ACME"
# ruleid: kit.configurable.no-banned-term
SUPPLIER = "Globex Corporation"
# ruleid: kit.configurable.no-banned-term
CATALOGUE_CODE = "SKU-1234"

# ok: kit.configurable.no-banned-term
NEUTRAL_CODE = "ABC-123"
# ok: kit.configurable.no-banned-term
SUBSTRING_ONLY = "acmeless"
# ok: kit.configurable.no-banned-term
SHORT_NUMBER = "SKU-12"
