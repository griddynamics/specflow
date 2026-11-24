"""The GitHub integration (docs/github/INDEX.md): the OAuth web flow of the GitHub App, the user access tokens and the
REST calls Specflow makes as the user. Stand-alone like ``jira``: no database, no workflow engine, no HTTP layer; the
connection rows reach it through the ``ConnectionStore`` port (``github.connections``)."""
