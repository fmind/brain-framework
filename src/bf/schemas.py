"""Offline editor schemas generated from the same models as configuration loading, and reply schemas."""

from typing import Literal

from bf.models import Config, UserConfig

Kind = Literal["brain", "registry", "eval", "search-reply", "read-reply"]


def document(kind: Kind = "brain") -> dict[str, object]:
    """Describe structural constraints; semantic validation remains with the owning command."""
    from bf.evaluate import Suite
    from bf.replies import READ, SEARCH

    if kind in ("search-reply", "read-reply"):
        # Replies are plain dictionaries: their schemas are declared, and tests validate real replies against them.
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **(SEARCH if kind == "search-reply" else READ),
        }
    model = {"brain": Config, "registry": UserConfig, "eval": Suite}[kind]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **model.model_json_schema()}
