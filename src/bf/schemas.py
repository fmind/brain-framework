"""Offline editor schemas generated from the same models as configuration loading, and reply schemas."""

from typing import Literal, cast

from bf.models import Config, UserConfig

Kind = Literal["brain", "registry", "eval", "search-reply", "read-reply"]


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
    from bf.evaluate import Suite
    from bf.replies import READ, SEARCH

    if kind in ("search-reply", "read-reply"):
        # Replies are plain dictionaries: their schemas are declared, and tests validate real replies against them.
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **cast("dict[str, object]", _open(SEARCH if kind == "search-reply" else READ)),
        }
    model = {"brain": Config, "registry": UserConfig, "eval": Suite}[kind]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **model.model_json_schema()}
