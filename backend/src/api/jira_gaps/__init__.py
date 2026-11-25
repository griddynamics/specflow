"""The Jira gap channel (docs/jira-sync/gaps.md): open questions posted as sub-tasks of one Story, answers read
back from their comments by polling. ``JiraGaps`` is held on ``Runtime.jira_gaps``."""

from __future__ import annotations

from api.jira_gaps.channel import JiraGaps

__all__ = ["JiraGaps"]
