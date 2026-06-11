"""Pydantic request models for the Finio API."""

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class CoachRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class GoalRequest(BaseModel):
    amount: float = Field(..., gt=0)
    target_date: date
    age: Optional[int] = Field(None, ge=16, le=100)


class SpendCheckRequest(BaseModel):
    merchant: str = ""
    amount: float = Field(..., gt=0)
    days_ahead: int = Field(30, ge=1, le=90)
