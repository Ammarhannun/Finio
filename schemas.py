"""Pydantic request models for the Finio API."""

import math
import re
from datetime import date
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Postgres cannot store a NUL byte in a text column — it raises 22P05,
# "\u0000 cannot be converted to text". A pasted message containing one used to
# reach append_chat and come back as a 500, so control characters are stripped
# at the edge. Tab and newline are kept; they are legitimate in a message.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(value):
    """Strip characters the database cannot store, and trim."""
    if not isinstance(value, str):
        return value
    return _CONTROL_CHARS.sub("", value).strip()


def _finite(value, *, field):
    """Reject inf/nan. They pass a `gt=0` check, survive into the arithmetic,
    and then produce either nonsense figures or JSON that cannot serialise."""
    if value is None:
        return value
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a real number")
    return number


# Money values above this are a typo or an attack, never a real budget or
# purchase, and they wreck every chart's scale.
MAX_MONEY = 100_000_000.0


class CoachRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)

    @field_validator("message", "page", "chat_id", mode="before")
    @classmethod
    def _strip_control(cls, v):
        return _clean_text(v)

    # Optional period so the coach can answer about the window the user is viewing.
    period: Optional[str] = None
    month: Optional[str] = None
    # Which page the user is on (dashboard/transactions/invest/…) so the coach
    # can tailor help to what's on screen.
    page: Optional[str] = Field(None, max_length=30)
    # Which conversation this message belongs to (multiple chats).
    chat_id: Optional[str] = Field(None, max_length=40)


class GoalRequest(BaseModel):
    amount: float = Field(..., gt=0, le=MAX_MONEY)
    target_date: date
    age: Optional[int] = Field(None, ge=16, le=100)

    @field_validator("amount")
    @classmethod
    def _real_number(cls, v):
        return _finite(v, field="amount")


class SpendCheckRequest(BaseModel):
    merchant: str = Field("", max_length=120)
    amount: float = Field(..., gt=0, le=MAX_MONEY)
    days_ahead: int = Field(30, ge=1, le=90)
    period: Optional[str] = None
    month: Optional[str] = None

    @field_validator("merchant", "period", "month", mode="before")
    @classmethod
    def _strip_control(cls, v):
        return _clean_text(v)

    @field_validator("amount")
    @classmethod
    def _real_number(cls, v):
        return _finite(v, field="amount")


class ProfileRequest(BaseModel):
    """Editable user details (profile page + signup). All optional so a partial
    update (e.g. just age at signup) is valid."""

    age: Optional[int] = Field(None, ge=16, le=100)
    income_bracket: Optional[str] = Field(None, max_length=40)
    custom_categories: Optional[List[str]] = None

    @field_validator("custom_categories")
    @classmethod
    def _sane_categories(cls, cats):
        if cats is None:
            return cats
        if len(cats) > 60:
            raise ValueError("too many custom categories")
        out = []
        for c in cats:
            name = _clean_text(c)
            if name and len(name) <= 60 and name not in out:
                out.append(name)
        return out



class BudgetRequest(BaseModel):
    """Set or clear monthly budget limits. `{"Groceries": 400}` sets one;
    a null value clears it back to the suggested baseline."""

    targets: Dict[str, Optional[float]] = Field(default_factory=dict)

    @field_validator("targets")
    @classmethod
    def _sane_targets(cls, targets):
        if len(targets) > 60:
            raise ValueError("too many budget categories")
        cleaned = {}
        for category, amount in targets.items():
            name = _clean_text(category)
            if not name or len(name) > 60:
                raise ValueError("bad category name")
            if amount is None:          # null clears the limit
                cleaned[name] = None
                continue
            value = _finite(amount, field=f"budget for {name}")
            if value < 0:
                raise ValueError("budgets cannot be negative")
            if value > MAX_MONEY:
                raise ValueError("that budget is unrealistically large")
            cleaned[name] = value
        return cleaned


class QuizRequest(BaseModel):
    """One quiz answer: categorise/flow a merchant, or skip the question."""

    merchant: str = Field(..., min_length=1, max_length=200)
    category: Optional[str] = Field(None, max_length=60)
    flow: Optional[Literal["income", "expense", "transfer"]] = None
    skip: bool = False

    @field_validator("merchant", "category", mode="before")
    @classmethod
    def _strip_control(cls, v):
        return _clean_text(v)


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

    @field_validator("match", "category", "tx_key", mode="before")
    @classmethod
    def _strip_control(cls, v):
        return _clean_text(v)

    @model_validator(mode="after")
    def _check(self):
        if not (self.match or self.tx_key):
            raise ValueError("rule needs a `match` or a `tx_key`")
        if self.flow is None and not (self.category and self.category.strip()):
            raise ValueError("rule needs a `flow` or a `category`")
        return self


class OverrideRequest(BaseModel):
    rules: List[OverrideRule] = Field(default_factory=list, max_length=5000)
    # Categories the user invented in the editor, persisted so the dropdown
    # keeps offering them even before/after they're assigned to a transaction.
    custom_categories: Optional[List[str]] = None

    @field_validator("custom_categories")
    @classmethod
    def _sane_categories(cls, cats):
        if cats is None:
            return cats
        if len(cats) > 60:
            raise ValueError("too many custom categories")
        out = []
        for c in cats:
            name = _clean_text(c)
            if name and len(name) <= 60 and name not in out:
                out.append(name)
        return out

