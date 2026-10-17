"""Validated provider-neutral channel envelopes; these carry data, never executable routes."""

from __future__ import annotations

from typing import Annotated
from unicodedata import category

from pydantic import Field, field_validator

from bf.models import Model, timestamp

# Messages one read batch carries; a read input splits a longer window.
MAX_BATCH = 1000


class Message(Model):
    """An authenticated, admitted message; the configured adapter owns sender authorization."""

    id: Annotated[str, Field(min_length=1, max_length=4096)]
    sender: Annotated[str, Field(min_length=1, max_length=4096)]
    conversation: Annotated[str, Field(min_length=1, max_length=4096)]
    time: str
    text: Annotated[str, Field(min_length=1, max_length=32_768)]
    reply_to: Annotated[str, Field(max_length=4096)] = ""
    cursor: Annotated[str, Field(max_length=4096)] = ""
    own: bool = False

    _time = field_validator("time")(timestamp)

    @field_validator("id", "sender", "conversation", "reply_to", "cursor")
    @classmethod
    def identity(cls, value: str) -> str:
        if any(category(char) in {"Cc", "Cf", "Cs"} for char in value) or (value and not value.strip()):
            raise ValueError("message identifiers cannot contain control characters")
        return value


class Batch(Model):
    messages: Annotated[list[Message], Field(max_length=MAX_BATCH)]


class Outgoing(Model):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    text: Annotated[str, Field(min_length=1, max_length=4 << 20)]
    conversation: Annotated[str, Field(max_length=4096)] = ""
    reply_to: Annotated[str, Field(max_length=4096)] = ""

    _identity = field_validator("id", "conversation", "reply_to")(Message.identity)


class Receipt(Model):
    id: Annotated[str, Field(min_length=1, max_length=4096)]
    conversation: Annotated[str, Field(min_length=1, max_length=4096)]

    _identity = field_validator("id", "conversation")(Message.identity)
