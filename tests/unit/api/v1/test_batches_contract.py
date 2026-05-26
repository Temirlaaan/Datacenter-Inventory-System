"""Contract test: the live 201 response of POST /api/v1/admin/batches/ must match
the schema FastAPI publishes in /openapi.json.

Catches silent drift when a later sprint changes the response shape without
updating downstream consumers. Deliberately minimal — checks required-field
presence and top-level field types, not a full JSON-Schema validation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.auth.dependencies import AuthUser

pytestmark = pytest.mark.integration

_JSON_TYPE_TO_PYTHON: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _resolve_ref(schema: dict[str, Any], openapi: dict[str, Any]) -> dict[str, Any]:
    """Follow a single ``$ref`` into components/schemas."""
    ref = schema.get("$ref")
    if ref is None:
        return schema
    name = ref.rsplit("/", 1)[-1]  # "#/components/schemas/Foo" -> "Foo"
    resolved: dict[str, Any] = openapi["components"]["schemas"][name]
    return resolved


async def test_create_batch_201_response_matches_openapi_schema(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")

    created = await client.post("/api/v1/admin/batches/", json={"count": 6})
    assert created.status_code == 201
    body = created.json()

    openapi = (await client.get("/openapi.json")).json()
    response_schema = openapi["paths"]["/api/v1/admin/batches/"]["post"]["responses"]["201"][
        "content"
    ]["application/json"]["schema"]
    schema = _resolve_ref(response_schema, openapi)

    # Every declared required field is present in the live body.
    for field in schema.get("required", []):
        assert field in body, f"required field {field!r} missing from response"

    # Each present field's runtime type matches the declared JSON-Schema type.
    for field, field_schema in schema["properties"].items():
        if field not in body or body[field] is None:
            continue
        declared = field_schema.get("type")
        if declared is None:  # e.g. a $ref or anyOf — out of scope for this check
            continue
        expected = _JSON_TYPE_TO_PYTHON[declared]
        assert isinstance(
            body[field], expected
        ), f"field {field!r}: expected {declared}, got {type(body[field]).__name__}"
