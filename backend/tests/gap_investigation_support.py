"""Investigation on the shop gap corpus for offline tests: runs with the structured loop over the recorded gateway, and
the tool that re-keys the recorded ``gap_investigate`` answers (docs/graph/gap/investigation.md; fixture README
"Recordings").

The recorded answers of ``recordings/gap_investigate.json`` are written by hand, one per site and round, with the note
``site <site id> round <n> (<detectors> <spec ids>)``. Their input hashes change whenever the prompt, the pack format or
the corpus changes; ``rerecord_investigations()`` runs a full run on the corpus, answers each call from the answer
whose note names the same site and round, and writes the file again with the new hashes:

    cd backend && PYTHONPATH=tests uv run python -c \
        "import asyncio, gap_investigation_support as s; asyncio.run(s.rerecord_investigations())"
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from gap_acceptance_support import RECORDINGS_DIR, RecordedGapGateway, gateway_role, input_hash, load_recordings
from gap_detection_support import run_detection
from gap_run_support import CountingInvestigator, GapWorld, corpus_world
from graph.gap import run_gap
from graph.gap.models import GapReport, InvestigationResult, SiteBudget, SitePack
from graph.gap.ports import PlanView, SiteReads
from graph.gap.prompts import INVESTIGATE_PROMPT
from graph.gap.steps.investigate import LoopInvestigator
from graph_support import ScriptedRequest

INVESTIGATE_FILE: Final = RECORDINGS_DIR / "gap_investigate.json"
_NOTE: Final = re.compile(r"^site (?P<site>SITE::[0-9A-F]+) round (?P<round>\d+)")


def pack_payload(request: ScriptedRequest) -> dict[str, Any]:
    """The JSON part of a round's first user message (the pack)."""

    user = str(request.messages[1]["content"])
    return dict(json.loads(user.split("\n\n")[1]))


def round_of(request: ScriptedRequest) -> int:
    """1 for the first call of a site, +1 per earlier round; the forced report call counts as a round."""

    return 1 + sum(1 for message in request.messages if message["role"] == "assistant")


def call_note(request: ScriptedRequest) -> str:
    payload = pack_payload(request)
    detectors = "+".join(sorted({seed["detector"] for seed in payload["seeds"]}))
    about = ",".join(sorted({spec_id for seed in payload["seeds"] for spec_id in seed["spec_ids"]})) or "-"
    return f"site {payload['site_id']} round {round_of(request)} ({detectors} {about})"


def note_key(note: str) -> tuple[str, int]:
    found = _NOTE.match(note)
    if found is None:
        raise ValueError(f"not a site note: {note!r}")
    return found["site"], int(found["round"])


def loop_world_of(world: GapWorld) -> GapWorld:
    """``world`` with the structured loop over its own gateway as investigator (counted)."""

    world.investigator = CountingInvestigator(LoopInvestigator(world.gateway))
    return world


async def loop_world(
    reader: Any = None, *, plan: PlanView | None = None, gateway: RecordedGapGateway | None = None
) -> GapWorld:
    """A corpus world whose investigator is the structured loop over the recorded gateway."""

    world = await corpus_world(reader, plan=plan)
    if gateway is not None:
        world.gateway = gateway
    return loop_world_of(world)


def answers_by_site(path: Path = INVESTIGATE_FILE) -> dict[tuple[str, int], tuple[str, Mapping[str, Any]]]:
    """The recorded answers by (site id, round), with their notes."""

    data = json.loads(path.read_text(encoding="utf-8"))
    found: dict[tuple[str, int], tuple[str, Mapping[str, Any]]] = {}
    for item in data["calls"]:
        found[note_key(item["note"])] = (item["note"], item["answer"])
    return found


async def rerecord_investigations(path: Path = INVESTIGATE_FILE) -> Sequence[str]:
    """Run a full run on the corpus answering every ``gap_investigate`` call from the answer of its site and round,
    and write ``path`` with the new input hashes. Returns the notes of calls nobody answered (an empty file entry is
    needed for each)."""

    authored = answers_by_site(path)
    recorded: list[dict[str, Any]] = []
    missing: list[str] = []

    def answer(request: ScriptedRequest) -> Any:
        note = call_note(request)
        site, round_no = note_key(note)
        found = authored.get((site, round_no))
        if found is None:
            missing.append(note)
            return {"action": "report", "reads": [], "report": {"verdict": "not_a_gap"}}
        recorded.append(
            {
                "input_hash": input_hash(str(gateway_role(request.prompt)), request.messages),
                "note": note,
                "answer": found[1],
            }
        )
        return dict(found[1])

    recordings = load_recordings()
    gateway = RecordedGapGateway(recordings={"gap_relevance": recordings["gap_relevance"]})
    gateway.handle(INVESTIGATE_PROMPT, answer)
    world = await loop_world(gateway=gateway)
    await run_gap(world.context(), mode="full")
    recorded.sort(key=lambda item: note_key(item["note"]))
    text = json.dumps({"role": "gap_investigate", "calls": recorded}, indent=2, ensure_ascii=False) + "\n"
    await asyncio.to_thread(path.write_text, text, encoding="utf-8")
    return missing


# Sites and scripted investigators


NOT_A_GAP: Final = GapReport(verdict="not_a_gap", why="Settled by the text.")


async def corpus_site_ids() -> dict[frozenset[str], str]:
    """The site ids of the shop corpus by the set of spec ids they are about (MS-COVERAGE sites by their fact)."""

    found = await run_detection()
    sites: dict[frozenset[str], str] = {}
    for site in found.sites.sites:
        key = frozenset(site.about) or frozenset(item.fact or "" for seed in site.seeds for item in seed.witness)
        sites[key] = site.site_id
    return sites


async def site_about(*spec_ids: str) -> str:
    return (await corpus_site_ids())[frozenset(spec_ids)]


class ReportsBySite:
    """An ``Investigator`` returning a prepared report per site id (``NOT_A_GAP`` for the others), no model call."""

    prompt_version = "scripted.1"

    def __init__(self, reports: Mapping[str, GapReport | InvestigationResult]) -> None:
        self.reports = dict(reports)
        self.packs: dict[str, SitePack] = {}

    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult:
        del reads, budget
        self.packs[pack.site_id] = pack
        found = self.reports.get(pack.site_id, NOT_A_GAP)
        return found if isinstance(found, InvestigationResult) else InvestigationResult(report=found, model_calls=1)


def recorded(gateway: RecordedGapGateway, request: ScriptedRequest) -> Any:
    """The recorded answer of ``request`` (for a handler that scripts some sites and replays the others)."""

    role = gateway_role(request.prompt)
    digest = input_hash(str(role), request.messages)
    found = gateway.recordings["gap_investigate"].get(digest)
    if found is None or found.answer is None:
        raise AssertionError(f"no recording for {call_note(request)}")
    return dict(found.answer)


def recording_of(site_id: str, round_no: int = 1) -> str:
    """The input hash recorded for a site's round in ``recordings/gap_investigate.json``."""

    data = json.loads(INVESTIGATE_FILE.read_text(encoding="utf-8"))
    for item in data["calls"]:
        if note_key(item["note"]) == (site_id, round_no):
            return str(item["input_hash"])
    raise KeyError(site_id)


def answer_by_site(request: ScriptedRequest) -> Any:
    """The recorded answer of a call's site and round, whatever its input hash (for live stores, whose packs read
    the core reader and so differ from the offline packs the hashes were taken on)."""

    found = answers_by_site().get((str(pack_payload(request)["site_id"]), round_of(request)))
    if found is None:
        raise AssertionError(f"no recorded answer for {call_note(request)}")
    return dict(found[1])
