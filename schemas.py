"""Pydantic request models for the Finio API."""

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class CoachRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    # Optional period so the coach can answer about the window the user is viewing.
    period: Optional[str] = None
    month: Optional[str] = None


class GoalRequest(BaseModel):
    amount: float = Field(..., gt=0)
    target_date: date
    age: Optional[int] = Field(None, ge=16, le=100)


class SpendCheckRequest(BaseModel):
    merchant: str = ""
    amount: float = Field(..., gt=0)
    days_ahead: int = Field(30, ge=1, le=90)
    period: Optional[str] = None
    month: Optional[str] = None


class OverrideRule(BaseModel):
    """Reclassify any transaction whose description contains `match`."""

    match: str = Field(..., min_length=1, max_length=200)
    flow: Literal["income", "expense", "transfer"]


class OverrideRequest(BaseModel):
    rules: List[OverrideRule] = Field(default_factory=list)
