"""Shared parts of the per-user OAuth connections (Atlassian, GitHub): the signed ``state``, the ``return_to`` rule,
the refresh-token cipher and the per-user access-token cache with one refresh at a time. No database, no HTTP."""
