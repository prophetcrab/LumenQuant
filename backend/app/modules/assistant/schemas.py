from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    account_id: str | None = None


class ToolCallRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)
