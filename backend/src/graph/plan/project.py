"""The Project step: the project profile, setup tasks and closing tasks, in one call."""

from __future__ import annotations

from graph.plan.answers import ProjectAnswer
from graph.plan.fallback import closing_story_id, project_fallback, setup_story_id
from graph.plan.identity import PROJECT_AREA_ID, task_key
from graph.plan.inputs import project_edges, project_inputs, project_messages, setup_members
from graph.plan.models import Convention, DescribeArtifact, PlannerOutcome, Profile, ProjectArtifact
from graph.plan.names import PlanMode
from graph.plan.normalise import normalise_project
from graph.plan.options import PlanContext
from graph.plan.planned import from_answer
from graph.plan.planners import PlannerCall, run_planner
from graph.plan.prompts import prompt_ref
from graph.plan.reuse import project_key
from graph.plan.validate import validate_project
from graph.plan.view import PlanView

PROJECT_KEY = PROJECT_AREA_ID


async def project(
    context: PlanContext, view: PlanView, describe: DescribeArtifact, *, mode: PlanMode
) -> ProjectArtifact:
    """Plan the project-wide work."""

    call = PlannerCall(
        kind="project",
        key=PROJECT_KEY,
        reuse_key=project_key(view, context.project_id, project_inputs(view), project_edges(view)),
        messages=project_messages(view, describe),
        answer_type=ProjectAnswer,
        validate=lambda answer: validate_project(answer, view=view, setup_members=setup_members(view)),
        normalise=lambda answer: normalise_project(answer, view=view),
    )
    answer, outcome = await run_planner(context, call, mode=mode)
    if answer is None:
        profile, setup, closing = project_fallback(view, setup_members(view))
        summaries = {
            story.story_id: story.summary
            for story in view.snapshot.current.stories
            if story.story_id in (setup_story_id(), closing_story_id()) and story.summary
        }
        return ProjectArtifact(profile=profile, setup=setup, closing=closing, summaries=summaries, outcome=outcome)
    return project_artifact(view, answer, outcome)


def project_artifact(view: PlanView, answer: ProjectAnswer, outcome: PlannerOutcome) -> ProjectArtifact:
    """The Project artifact of a valid answer."""

    setup_keys = {task.action: task_key(PROJECT_AREA_ID, task.action) for task in answer.setup_tasks}
    ref = prompt_ref("project")
    setup = tuple(
        from_answer(
            view, task, area_id=PROJECT_AREA_ID, story_id=setup_story_id(), prompt_ref=ref, wait_keys=setup_keys
        )
        for task in answer.setup_tasks
    )
    closing = tuple(
        from_answer(view, task, area_id=PROJECT_AREA_ID, story_id=closing_story_id(), prompt_ref=ref, wait_keys={})
        for task in answer.closing_tasks
    )
    summaries = {
        story: text.strip()
        for story, text in ((setup_story_id(), answer.setup_summary), (closing_story_id(), answer.closing_summary))
        if text.strip()
    }
    return ProjectArtifact(profile=_profile(answer), setup=setup, closing=closing, summaries=summaries, outcome=outcome)


def _profile(answer: ProjectAnswer) -> Profile:
    profile = answer.profile
    return Profile(
        languages=tuple(profile.languages),
        frameworks=tuple(profile.frameworks),
        layout=profile.layout or None,
        test_command=profile.test_command or None,
        test_paths=tuple(profile.test_paths),
        spec_ids=tuple(sorted(set(profile.spec_ids))),
        conventions=tuple(
            Convention(text=item.text, spec_ids=tuple(sorted(set(item.spec_ids))))
            for item in profile.conventions
            if item.text.strip()
        ),
    )
