"""Offline editor schemas generated from the same models as configuration loading, and reply schemas."""

from typing import cast

from bf.models import Config, UserConfig
from bf.models import SchemaKind as Kind


def _open(value: object) -> object:
    """A reply schema that tolerates fields added later: consumers ignore what they do not know.

    Tests validate every reply against the strict declaration in `bf.replies`, so an undeclared field still fails
    there; the published form lets a minor release add fields without breaking a consumer's validation. Once
    shapes accept extra fields, one reply can match several of them (an empty period page is also a listing), so
    their `oneOf` becomes `anyOf`.
    """
    if isinstance(value, dict):
        return {
            ("anyOf" if key == "oneOf" else key): _open(item)
            for key, item in value.items()
            if not (key == "additionalProperties" and item is False)
        }
    if isinstance(value, list):
        return [_open(item) for item in value]
    return value


def document(kind: Kind = "brain") -> dict[str, object]:
    """Describe structural constraints; semantic validation remains with the owning command."""
    if kind == "assistant-reply":
        from bf.replies import ASSISTANT

        # Strict on purpose: a harness's structured-output mode needs every property declared and no extra ones.
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", **ASSISTANT}
    if kind in ("search-reply", "read-reply", "status-reply"):
        from bf.replies import READ, SEARCH, STATUS

        # Replies are plain dictionaries: their schemas are declared, and tests validate real replies against them.
        reply = {"search-reply": SEARCH, "read-reply": READ, "status-reply": STATUS}[kind]
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", **cast("dict[str, object]", _open(reply))}
    if kind == "eval":
        from bf.evaluate import Suite

        return {"$schema": "https://json-schema.org/draft/2020-12/schema", **Suite.model_json_schema()}
    model = {"brain": Config, "registry": UserConfig}[kind]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **model.model_json_schema()}
