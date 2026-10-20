from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    account_id: str | None = None


class ToolCallRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)
