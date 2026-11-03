"""Structured answers of the planners, as the gateway validates them against a strict JSON schema.

Every field is required in the schema; vocabulary fields are plain strings here, so a value outside the
vocabulary is a validation problem the planner may repair, not a malformed answer.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Answer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DoneWhenAnswer(_Answer):
    """One completion statement; ``source`` is ``spec`` or ``planner``."""

    text: str
    source: str


class TaskAnswer(_Answer):
    """One planned task; ``waits_for`` names actions of the same area (or setup actions)."""

    story: str
    action: str
    phase: str
    title: str
    description: str
    implements: list[str]
    waits_for: list[str]
    done_when: list[DoneWhenAnswer]
    complexity: str


class NotBuildableAnswer(_Answer):
    """A member that is no code work."""

    spec_id: str
    reason: str
    note: str


class TasksAnswer(_Answer):
    """The tasks of one planning part."""

    tasks: list[TaskAnswer]
    not_buildable: list[NotBuildableAnswer]


class InterfaceAnswer(_Answer):
    """An interface of an area; ``spec_id`` is empty for one the describer adds."""

    spec_id: str
    name: str
    kind: str
    summary: str
    spec_ids: list[str]


class StorySummaryAnswer(_Answer):
    story_id: str
    summary: str


class DescribeAnswer(_Answer):
    """Title, description, interfaces and story summaries of one area."""

    title: str
    description: str
    interfaces: list[InterfaceAnswer]
    stories: list[StorySummaryAnswer]


class ConventionAnswer(_Answer):
    text: str
    spec_ids: list[str]


class ProfileAnswer(_Answer):
    """The project profile every task follows."""

    languages: list[str]
    frameworks: list[str]
    layout: str
    test_command: str
    test_paths: list[str]
    spec_ids: list[str]
    conventions: list[ConventionAnswer]


class ProjectAnswer(_Answer):
    """The project profile, setup and closing tasks; ``story`` of these tasks is ignored."""

    profile: ProfileAnswer
    setup_tasks: list[TaskAnswer]
    closing_tasks: list[TaskAnswer]
    setup_summary: str
    closing_summary: str


class ReviewIssueAnswer(_Answer):
    """One advisory finding; ``task_id`` is empty for an area-level issue."""

    area_id: str
    task_id: str
    problem: str
    severity: str


class ReviewAnswer(_Answer):
    issues: list[ReviewIssueAnswer]
