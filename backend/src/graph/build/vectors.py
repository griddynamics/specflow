"""Embedding vectors of a build, stored once per distinct embedded text (build.md "Reuse in update builds").

Step artifacts never hold vectors: they keep the embedded text, and operations keep its sha256
(``text_hash``). Every step gets its vectors through a ``VectorCache``: first its in-memory memo, then
the ``ExtractionStore`` (kind ``vectors``), then ``Gateway.embed`` for the rest, which are written back
in one batch. Fragment vectors (known-term ranking), hub vectors, ``name_embedding`` (Resolve, edge
catalogs, Conform) and the search ``embedding`` all share this one store.

Store entry: key ``<project_id>|<embedding_model>|<sha256(text)>`` (the store is shared by the
projects under one root), fingerprint the embedding model, payload ``{"dim": n, "f32": base64}`` with
the vector's little-endian float32 bytes. An entry of another dimension, or one that does not decode,
is a miss. In memory vectors are float32 ``numpy`` arrays, so a vector read back from the store equals
the one first embedded.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from typing import Final

import numpy as np

from graph.build.ports import ExtractionKind, ExtractionStore, JsonObject
from graph.core.errors import ConformanceError
from graph.core.llm import Gateway
from logger.logger import DEBUG, WARNING, get_logger, log_event

type Vector = Sequence[float] | np.ndarray
"""A vector as the build passes it around: a float sequence or a numpy array."""

logger = get_logger("graph.build.vectors")

VECTOR_KIND: Final[ExtractionKind] = "vectors"
_FLOAT32_LE: Final = np.dtype("<f4")


def text_hash(text: str) -> str:
    """sha256 (hex) of the UTF-8 bytes of an embedded text."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_vector(vector: np.ndarray) -> JsonObject:
    """The store payload of one vector: its dimension and base64 little-endian float32 bytes."""

    data = np.asarray(vector, dtype=_FLOAT32_LE)
    return {"dim": int(data.shape[0]), "f32": base64.b64encode(data.tobytes()).decode("ascii")}


def decode_vector(payload: JsonObject, dim: int) -> np.ndarray | None:
    """The float32 vector of a store payload; None when it is malformed or not of dimension ``dim``."""

    encoded = payload.get("f32")
    if payload.get("dim") != dim or not isinstance(encoded, str):
        return None
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        return None
    if len(raw) != dim * _FLOAT32_LE.itemsize:
        return None
    return np.frombuffer(raw, dtype=_FLOAT32_LE).astype(np.float32)


async def get_many(
    store: ExtractionStore, kind: ExtractionKind, keys: Sequence[str], fingerprint: str
) -> dict[str, JsonObject]:
    """Stored payloads by key (absent keys left out), in one call when the store offers ``get_many``
    (``BatchExtractionStore``; the file store does), else one ``get`` per key."""

    if not keys:
        return {}
    batch = getattr(store, "get_many", None)
    if callable(batch):
        found = await batch(kind, keys, fingerprint)
        return {key: found[key] for key in keys if key in found}
    result: dict[str, JsonObject] = {}
    for key in keys:
        payload = await store.get(kind, key, fingerprint)
        if payload is not None:
            result[key] = payload
    return result


async def put_many(
    store: ExtractionStore, kind: ExtractionKind, fingerprint: str, entries: Mapping[str, JsonObject]
) -> None:
    """Store ``entries`` (payload by key) in one call when the store offers ``put_many``
    (``BatchExtractionStore``; the file store writes one pass without an fsync per entry), else one
    ``put`` per entry."""

    if not entries:
        return
    batch = getattr(store, "put_many", None)
    if callable(batch):
        await batch(kind, fingerprint, entries)
        return
    for key, payload in entries.items():
        await store.put(kind, key, fingerprint, payload)


class VectorCache:
    """Vectors of embedded texts for one project and embedding model (see the module docstring).

    ``get_or_embed`` serves any text; ``by_hash`` serves operations that carry only ``text_hash``
    values (Persist). The memo keeps every vector served by ``get_or_embed`` for the cache's lifetime
    (one step); ``by_hash`` does not fill it.
    """

    def __init__(self, gateway: Gateway, store: ExtractionStore, project_id: str) -> None:
        self._gateway = gateway
        self._store = store
        self._project_id = project_id
        self._model = gateway.config.embedding_model
        self._dim = gateway.config.embedding_dim
        self._memo: dict[str, np.ndarray] = {}

    @property
    def model(self) -> str:
        """The embedding model the vectors come from."""

        return self._model

    def key(self, digest: str) -> str:
        """Store key of the text whose sha256 is ``digest``."""

        return f"{self._project_id}|{self._model}|{digest}"

    async def get_or_embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        """The float32 vector of each text, in order; each distinct text is embedded at most once and
        only when neither the memo nor the store holds it; new vectors are stored in one batch."""

        digests = [text_hash(text) for text in texts]
        wanted = list(dict.fromkeys(digest for digest in digests if digest not in self._memo))
        stored = await self._read(wanted)
        self._memo.update(stored)
        texts_by_digest = dict(zip(digests, texts, strict=True))
        missing = [digest for digest in wanted if digest not in stored]
        if missing:
            fresh = await self._gateway.embed([texts_by_digest[digest] for digest in missing])
            vectors = [np.asarray(vector, dtype=np.float32) for vector in fresh]
            self._memo.update(zip(missing, vectors, strict=True))
            await put_many(
                self._store,
                VECTOR_KIND,
                self._model,
                {self.key(digest): encode_vector(vector) for digest, vector in zip(missing, vectors, strict=True)},
            )
        log_event(
            logger,
            DEBUG,
            "graph.vectors.served",
            texts=len(texts),
            distinct=len(set(digests)),
            stored=len(stored),
            embedded=len(missing),
        )
        return [self._memo[digest] for digest in digests]

    async def mapping(self, texts: Iterable[str]) -> dict[str, np.ndarray]:
        """``get_or_embed`` of the distinct ``texts`` (sorted) as a text-to-vector mapping."""

        ordered = sorted(set(texts))
        return dict(zip(ordered, await self.get_or_embed(ordered), strict=True)) if ordered else {}

    async def stored(self, texts: Iterable[str]) -> set[str]:
        """The distinct ``texts`` whose vector the memo or the store holds; reads only, embeds nothing
        (``estimate_build``)."""

        by_digest = {text_hash(text): text for text in texts}
        found = await self._read([digest for digest in by_digest if digest not in self._memo])
        return {text for digest, text in by_digest.items() if digest in self._memo or digest in found}

    async def by_hash(
        self, digests: Collection[str], texts: Callable[[], Awaitable[Mapping[str, str]]]
    ) -> dict[str, np.ndarray]:
        """Vectors by ``text_hash`` from the store; only when some are missing, ``texts()`` (text by
        hash, e.g. from the target graph) is read and those are embedded again and stored. A hash
        found in neither raises ``ConformanceError("vector_missing")``."""

        ordered = sorted(set(digests))
        found = await self._read(ordered)
        missing = [digest for digest in ordered if digest not in found]
        if not missing:
            return found
        known = await texts()
        unknown = [digest for digest in missing if digest not in known]
        if unknown:
            raise ConformanceError("vector_missing", missing=len(unknown))
        found.update(zip(missing, await self.get_or_embed([known[digest] for digest in missing]), strict=True))
        log_event(logger, WARNING, "graph.vectors.reembedded", vectors=len(missing))
        return found

    async def _read(self, digests: Sequence[str]) -> dict[str, np.ndarray]:
        keys = {self.key(digest): digest for digest in digests}
        payloads = await get_many(self._store, VECTOR_KIND, list(keys), self._model)
        result: dict[str, np.ndarray] = {}
        for key, payload in payloads.items():
            vector = decode_vector(payload, self._dim)
            if vector is not None:
                result[keys[key]] = vector
        return result
