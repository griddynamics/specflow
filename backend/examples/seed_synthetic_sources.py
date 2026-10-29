"""Fill a scratch database at revision 0004 with synthetic sources data (migration rehearsal).

Run from ``backend/``, against a LOCAL scratch database that is migrated to 0004 and
empty (SPECS sources-ux 3.2 "Rehearsal", H1(01)):

    uv run --env-file ../.env.local examples/seed_synthetic_sources.py --database specflow_rehearsal
    uv run --env-file ../.env.local examples/seed_synthetic_sources.py --database specflow_rehearsal --counts

It writes plain SQL rows only. It never touches GCS, Temporal, Drive or any other service,
and it never reads production data: every name, hash and key is made up. All object keys it
stores are fake and point to nothing.

Data (default scale 1.0, deterministic for one ``--seed``):
- 30 projects, 12 users;
- 20,000 upload documents over 60 UTC days (5 % without uploader, 10 % failed, a few still
  uploaded or stuck processing);
- 15 Drive sources of 200-1,000 items, one of exactly 1,000 (the H3 perf source), in every
  item state (pending, processed, stored, skipped, error with each reason);
- about 5 % of the items of later sources link a document an earlier source of the same
  project already holds, as processed or as an old error/parse_failed link.

Output, to stdout, one JSON object per line: the row count of every table
(``{"connection_items": n, ...}``, sorted keys, so two runs diff mechanically), then, after a
seed, a summary of what was generated (counts and ids only, no names).

Safety (each refusal exits 2): ``--database`` is required and must start with
``specflow_rehearsal`` or ``specflow_pytest_``; the host must be local (localhost, 127.0.0.1
or ::1); a seed needs ``alembic_version = '0004'`` and no user yet. ``--counts`` only reads.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select, table, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import build_engine, build_session_factory
from settings import DatabaseConfigurationError, DatabaseSettings

SAFE_NAMES: Final = ("specflow_rehearsal", "specflow_pytest_")
LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
REVISION: Final = "0004"
TABLES: Final = (
    "users",
    "projects",
    "project_members",
    "documents",
    "connections",
    "connection_items",
)
CHUNK: Final = 2_000

# Shares and thresholds of the generated upload and Drive item mix.
UPLOAD_FAILED_SHARE: Final = 0.10
UPLOAD_PARSE_FAILED_SHARE: Final = 0.5
UPLOAD_UPLOADED_SHARE: Final = 0.13
UPLOAD_PROCESSING_SHARE: Final = 0.15
UPLOAD_PDF_SHARE: Final = 0.99
UPLOAD_NO_AUTHOR_SHARE: Final = 0.05
IMPORTING_PENDING_SHARE: Final = 0.30
ITEM_ERROR_SHARE: Final = 0.07
ITEM_DUPLICATE_SHARE: Final = 0.06
DUPLICATE_UNREADABLE_SHARE: Final = 0.4
FAILED_SOURCE_PROCESSED_SHARE: Final = 0.5

# (category, mime type, processing class, share of the items)
KINDS: Final = (
    ("pdf", "application/pdf", "processed", 0.45),
    ("google_docs", "application/vnd.google-apps.document", "processed", 0.20),
    ("word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "processed", 0.10),
    ("images", "image/png", "stored_only", 0.12),
    ("videos", "video/mp4", "stored_only", 0.03),
    ("other_google", "application/vnd.google-apps.form", "skipped", 0.10),
)
ERROR_REASONS: Final = (
    ("no_access", "download"),
    ("drive_unavailable", "download"),
    ("convert_failed", "convert"),
    ("parse_failed", "parse"),
    ("store_failed", "store"),
    ("import_failed", None),
    ("unknown_error", None),
)
INSERT_USER: Final = text("INSERT INTO users (id, actor_id, display_name, email) VALUES (:id, :actor, :name, :email)")
INSERT_PROJECT: Final = text("INSERT INTO projects (id, name, created_by) VALUES (:id, :name, :by)")
INSERT_MEMBER: Final = text("INSERT INTO project_members (project_id, user_id) VALUES (:p, :u)")
INSERT_DOCUMENT: Final = text(
    "INSERT INTO documents (id, project_id, content_hash, original_filename, media_type, size_bytes, "
    "origin, original_key, status, error, uploaded_by, created_at, updated_at) VALUES (:id, :p, :hash, "
    ":name, :media, :size, :origin, :key, :status, :error, :by, :at, :updated)"
)
INSERT_CONNECTION: Final = text(
    "INSERT INTO connections (id, project_id, kind, external_id, link, name, state, failure_reason, "
    "connected_by, created_at, updated_at) VALUES (:id, :p, 'gdrive', :ext, :link, :name, :state, "
    ":reason, :by, :at, :at)"
)
INSERT_ITEM: Final = text(
    "INSERT INTO connection_items (id, connection_id, project_id, external_id, name, mime_type, "
    "category, processing_class, size_bytes, state, error_reason, error_step, document_id, "
    "created_at, updated_at) VALUES (:id, :c, :p, :ext, :name, :mime, :cat, :cls, :size, :state, "
    ":reason, :step, :doc, :at, :at)"
)


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def chunks(rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(rows), CHUNK):
        yield rows[start : start + CHUNK]


class SeededRandom(random.Random):
    """Deterministic generator for reproducible synthetic rows (never used for anything security related)."""


class Generator:
    """Builds every row in memory with one seeded random source."""

    def __init__(self, seed: int, scale: float, now: datetime) -> None:
        self.rng = SeededRandom(seed)
        self.scale = scale
        self.now = now
        self.users: list[dict[str, Any]] = []
        self.projects: list[dict[str, Any]] = []
        self.members: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.connections: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.held: dict[uuid.UUID, list[uuid.UUID]] = {}  # project -> Drive documents so far
        self.perf_source: uuid.UUID | None = None

    def new_id(self) -> uuid.UUID:
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)

    def build(self) -> None:
        self._people()
        self._uploads()
        self._drive_sources()

    def _people(self) -> None:
        user_count = max(3, round(12 * self.scale))
        for n in range(user_count):
            self.users.append(
                {
                    "id": self.new_id(),
                    "actor": f"rehearsal-user-{n:02d}",
                    "name": f"Rehearsal User {n:02d}",
                    "email": f"rehearsal-{n:02d}@example.invalid",
                }
            )
        for n in range(max(6, round(30 * self.scale))):
            owner = self.users[n % len(self.users)]
            project = {
                "id": self.new_id(),
                "name": f"Rehearsal project {n:02d}",
                "by": owner["id"],
            }
            self.projects.append(project)
            for user in {owner["id"], self.users[(n + 1) % len(self.users)]["id"]}:
                self.members.append({"p": project["id"], "u": user})

    def _members_of(self, project: dict[str, Any]) -> list[uuid.UUID]:
        return [m["u"] for m in self.members if m["p"] == project["id"]]

    def _uploads(self) -> None:
        base = (self.now - timedelta(days=60)).replace(hour=0, minute=0, second=0, microsecond=0)
        members = {p["id"]: self._members_of(p) for p in self.projects}
        for n in range(round(20_000 * self.scale)):
            project = self.projects[int(self.rng.triangular(0, len(self.projects), 0)) % len(self.projects)]
            created = base + timedelta(days=self.rng.randrange(60), seconds=self.rng.randrange(86_400))
            roll = self.rng.random()
            status, error, age = (
                "parsed",
                None,
                timedelta(seconds=self.rng.randrange(5, 600)),
            )
            if roll < UPLOAD_FAILED_SHARE:
                status = "failed"
                error = "pdf_parse_failed" if self.rng.random() < UPLOAD_PARSE_FAILED_SHARE else "workflow_start_failed"
            elif roll < UPLOAD_UPLOADED_SHARE:
                status, age = "uploaded", timedelta()
            elif roll < UPLOAD_PROCESSING_SHARE:
                status, age = (
                    "processing",
                    timedelta(),
                )  # stuck: long past the stale limit
            media = "application/pdf" if self.rng.random() < UPLOAD_PDF_SHARE else "text/plain"
            document_id = self.new_id()
            self.documents.append(
                {
                    "id": document_id,
                    "p": project["id"],
                    "hash": sha(f"upload-{n}"),
                    "name": f"upload-{n:06d}.pdf",
                    "media": media,
                    "size": self.rng.randrange(10_000, 5_000_000),
                    "origin": "upload",
                    "key": f"rehearsal/{project['id']}/{document_id}/original.pdf",
                    "status": status,
                    "error": error,
                    "by": None
                    if self.rng.random() < UPLOAD_NO_AUTHOR_SHARE
                    else self.rng.choice(members[project["id"]]),
                    "at": created,
                    "updated": created + age,
                }
            )

    def _item_kind(self) -> tuple[str, str, str]:
        roll, edge = self.rng.random(), 0.0
        for category, mime, cls, share in KINDS:
            edge += share
            if roll < edge:
                return category, mime, cls
        return KINDS[0][:3]

    def _drive_sources(self) -> None:
        sizes = [1000] + [self.rng.randrange(200, 1001) for _ in range(14)]
        source_count = max(2, round(15 * self.scale))
        project_pool = self.projects[:6]
        for index in range(source_count):
            size = (
                sizes[index % len(sizes)] if self.scale >= 1 else max(20, round(sizes[index % len(sizes)] * self.scale))
            )
            project = project_pool[index % len(project_pool)]
            state, reason = "done", None
            if index == source_count - 2:
                state = "importing"
            elif index == source_count - 1:
                state, reason = "failed", "no_access"
            created = self.now - timedelta(days=50 - index * 3, hours=index)
            source_id = self.new_id()
            if index == 0:
                self.perf_source = source_id
            folder = f"rehearsal-folder-{index:02d}-{source_id.hex[:12]}"
            self.connections.append(
                {
                    "id": source_id,
                    "p": project["id"],
                    "ext": folder,
                    "link": f"https://drive.google.com/drive/folders/{folder}",
                    "name": f"Rehearsal folder {index:02d}",
                    "state": state,
                    "reason": reason,
                    "by": project["by"],
                    "at": created,
                }
            )
            self._items(source_id, project, index, size, source_state=state, created=created)

    def _items(
        self,
        source_id: uuid.UUID,
        project: dict[str, Any],
        index: int,
        size: int,
        *,
        source_state: str,
        created: datetime,
    ) -> None:
        earlier = self.held.setdefault(project["id"], [])
        mine: list[uuid.UUID] = []
        for j in range(size):
            category, mime, cls = self._item_kind()
            row: dict[str, Any] = {
                "id": self.new_id(),
                "c": source_id,
                "p": project["id"],
                "ext": f"drv-{index:02d}-{j:05d}",
                "name": f"Rehearsal file {index:02d}-{j:05d}",
                "mime": mime,
                "cat": category,
                "cls": cls,
                "size": self.rng.randrange(1_000, 3_000_000),
                "state": "pending",
                "reason": None,
                "step": None,
                "doc": None,
                "at": created + timedelta(seconds=j // 20),
            }
            roll = self.rng.random()
            if source_state == "importing" and roll < IMPORTING_PENDING_SHARE:
                pass  # still pending
            elif cls == "skipped":
                row["state"] = "skipped"
            elif cls == "stored_only":
                row["state"] = "stored"
            elif roll < ITEM_ERROR_SHARE:
                row["state"] = "error"
                row["reason"], row["step"] = self.rng.choice(ERROR_REASONS)
            elif earlier and self.rng.random() < ITEM_DUPLICATE_SHARE:
                # a cross-folder duplicate: links a document an earlier source holds
                row["doc"] = self.rng.choice(earlier)
                if self.rng.random() < DUPLICATE_UNREADABLE_SHARE:  # the old shape of an unreadable duplicate
                    row["state"], row["reason"], row["step"] = (
                        "error",
                        "parse_failed",
                        "parse",
                    )
                else:
                    row["state"] = "processed"
            else:
                row["state"] = (
                    "processed" if source_state != "failed" or roll < FAILED_SOURCE_PROCESSED_SHARE else "error"
                )
                if row["state"] == "error":
                    row["reason"], row["step"] = "import_failed", None
                else:
                    doc = self.new_id()
                    row["doc"] = doc
                    mine.append(doc)
                    self.documents.append(
                        {
                            "id": doc,
                            "p": project["id"],
                            "hash": sha(f"drive-{index}-{j}"),
                            "name": row["name"],
                            "media": mime,
                            "size": row["size"],
                            "origin": "connection",
                            "key": f"rehearsal/{project['id']}/{doc}/original",
                            "status": "parsed",
                            "error": None,
                            "by": None,
                            "at": row["at"],
                            "updated": row["at"],
                        }
                    )
            self.items.append(row)
        earlier.extend(mine)  # only later sources link these documents

    def summary(self) -> dict[str, Any]:
        return {
            "seed_summary": {
                "upload_documents": sum(1 for d in self.documents if d["origin"] == "upload"),
                "drive_documents": sum(1 for d in self.documents if d["origin"] == "connection"),
                "drive_sources": len(self.connections),
                "perf_source": {
                    "project_id": str(self._perf_project()),
                    "source_id": str(self.perf_source),
                    "items": 1000 if self.scale >= 1 else None,
                },
                "items_by_state": dict(sorted(Counter(i["state"] for i in self.items).items())),
                "upload_documents_by_status": dict(
                    sorted(Counter(d["status"] for d in self.documents if d["origin"] == "upload").items())
                ),
                "linked_items": sum(1 for i in self.items if i["doc"] is not None),
            }
        }

    def _perf_project(self) -> uuid.UUID:
        project: uuid.UUID = next(c["p"] for c in self.connections if c["id"] == self.perf_source)
        return project


async def table_counts(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in TABLES:
        counts[name] = (await session.execute(select(func.count()).select_from(table(name)))).scalar_one()
    return counts


async def insert(session: AsyncSession, statement: Any, rows: list[dict[str, Any]]) -> None:
    for part in chunks(rows):
        await session.execute(statement, part)


async def seed(session: AsyncSession, generator: Generator) -> None:
    await insert(session, INSERT_USER, generator.users)
    await insert(session, INSERT_PROJECT, generator.projects)
    await insert(session, INSERT_MEMBER, generator.members)
    await insert(session, INSERT_CONNECTION, generator.connections)
    await insert(session, INSERT_DOCUMENT, generator.documents)
    await insert(session, INSERT_ITEM, generator.items)


def refusal(settings: DatabaseSettings) -> str | None:
    if not settings.name.startswith(SAFE_NAMES):
        return f"the database name must start with one of {SAFE_NAMES}"
    if settings.host not in LOCAL_HOSTS:
        return "the database host must be local (localhost, 127.0.0.1 or ::1)"
    return None


async def run(settings: DatabaseSettings, args: argparse.Namespace) -> int:
    engine = build_engine(settings)
    try:
        async with build_session_factory(engine)() as session, session.begin():
            generator: Generator | None = None
            if not args.counts:
                version: list[str] = list(
                    (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
                )
                if version != [REVISION]:
                    sys.stderr.write(f"refused: the database must be at revision {REVISION}\n")
                    return 2
                if (await table_counts(session))["users"]:
                    sys.stderr.write("refused: the database is not empty\n")
                    return 2
                generator = Generator(args.seed, args.scale, datetime.now(UTC))
                generator.build()
                await seed(session, generator)
            sys.stdout.write(json.dumps(await table_counts(session), sort_keys=True) + "\n")
            if generator is not None:
                sys.stdout.write(json.dumps(generator.summary(), sort_keys=True) + "\n")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, help="scratch database name (specflow_rehearsal*)")
    parser.add_argument("--counts", action="store_true", help="print the row counts and change nothing")
    parser.add_argument("--seed", type=int, default=20261021)
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="share of the full data set (0.05 for a smoke run)",
    )
    args = parser.parse_args(argv)
    if not 0 < args.scale <= 1:
        sys.stderr.write("refused: --scale must be in (0, 1]\n")
        return 2
    try:
        settings = DatabaseSettings.from_environment().model_copy(update={"name": args.database})
    except DatabaseConfigurationError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    problem = refusal(settings)
    if problem is not None:
        sys.stderr.write(f"refused: {problem}\n")
        return 2
    return asyncio.run(run(settings, args))


if __name__ == "__main__":
    sys.exit(main())
