"""The request and response bodies of the Linear connection routes (``api.routers.linear``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.schemas import same_site_path
from integrations.return_to import RETURN_TO_MAX


class LinearWorkspaceOut(BaseModel):
    id: str
    name: str
    url_key: str


class LinearConnectionOut(BaseModel):
    """The caller's Linear connection; ``available`` is whether the server has the Linear app configured."""

    available: bool
    status: Literal["none", "active", "expired", "revoked"]
    user_name: str | None
    workspace: LinearWorkspaceOut | None


class LinearAuthorizeIn(BaseModel):
    """Where the browser returns after the consent: a path of this site (the Atlassian rule)."""

    model_config = ConfigDict(extra="forbid")

    return_to: str = Field(min_length=1, max_length=RETURN_TO_MAX)

    @field_validator("return_to")
    @classmethod
    def _same_origin_path(cls, value: str) -> str:
        return same_site_path(value)


class LinearAuthorizeOut(BaseModel):
    url: str


class LinearCallbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)


class LinearCallbackOut(BaseModel):
    connection: LinearConnectionOut
    return_to: str
