"""Pydantic request models for the Finio API."""

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


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


class ProfileRequest(BaseModel):
    """Editable user details (profile page + signup). All optional so a partial
    update (e.g. just age at signup) is valid."""

    age: Optional[int] = Field(None, ge=16, le=100)
    income_bracket: Optional[str] = Field(None, max_length=40)
    custom_categories: Optional[List[str]] = None


class OverrideRule(BaseModel):
    """Reclassify transactions. A rule targets either one transaction by
    `tx_key` (precise, single-row / multi-select edits) or every transaction
    whose description contains `match` (the original text rule). It can change
    the `flow`, the `category`, or both.
    """

    match: Optional[str] = Field(None, max_length=200)
    tx_key: Optional[str] = Field(None, max_length=64)
    flow: Optional[Literal["income", "expense", "transfer"]] = None
    category: Optional[str] = Field(None, max_length=60)

    @model_validator(mode="after")
    def _check(self):
        if not (self.match or self.tx_key):
            raise ValueError("rule needs a `match` or a `tx_key`")
        if self.flow is None and not (self.category and self.category.strip()):
            raise ValueError("rule needs a `flow` or a `category`")
        return self


class OverrideRequest(BaseModel):
    rules: List[OverrideRule] = Field(default_factory=list)
    # Categories the user invented in the editor, persisted so the dropdown
    # keeps offering them even before/after they're assigned to a transaction.
    custom_categories: Optional[List[str]] = None
