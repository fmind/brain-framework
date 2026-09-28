"""Offline editor schemas generated from the same models as configuration loading."""

from typing import Literal

from bf.models import Config, UserConfig

Kind = Literal["brain", "watch", "registry", "eval"]


def document(kind: Kind = "brain") -> dict[str, object]:
    """Describe structural constraints; semantic validation remains with the owning command."""
    from bf.evaluate import Suite
    from bf.watch_settings import Settings

    model = {"brain": Config, "watch": Settings, "registry": UserConfig, "eval": Suite}[kind]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **model.model_json_schema()}
