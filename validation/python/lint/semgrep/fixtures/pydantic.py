"""Fixtures for semgrep/pydantic.yml: positive and negative
cases for the rules."""

from typing import Any
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter()


# ruleid: kit.pydantic.no-nested-config-class
class LegacyModel(BaseModel):
    name: str

    class Config:
        frozen = True


# ok: kit.pydantic.no-nested-config-class
class CurrentModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str


class SchemaModel(BaseModel):
    # ruleid: kit.pydantic.no-loose-dict-field
    bad_dict: dict[str, Any]

    # ruleid: kit.pydantic.no-loose-dict-field
    bad_str_dict: dict[str, str] = {}

    # ruleid: kit.pydantic.no-loose-dict-field
    bad_list_dict: list[dict[str, Any]] = []

    # ruleid: kit.pydantic.no-any-field
    bad_any: Any

    # ruleid: kit.pydantic.no-any-field
    bad_any_list: list[Any] = []

    # ruleid: kit.pydantic.no-loose-dict-field
    params: dict[str, str] = {}

    # ruleid: kit.pydantic.no-loose-dict-field
    last_error_details: dict[str, str] | None = None

    # ok: kit.pydantic.no-loose-dict-field
    # ok: kit.pydantic.no-any-field
    clean_field: int = 1


# ruleid: kit.pydantic.no-untyped-route-response
@router.get("/bad")
async def bad_route() -> dict[str, Any]:
    return {}


# ruleid: kit.pydantic.no-untyped-route-response
@router.get("/bad-any")
async def bad_any_route() -> Any:
    return {}


# ok: kit.pydantic.no-untyped-route-response
@router.get("/good")
async def good_route() -> CurrentModel:
    return CurrentModel(name="test")
