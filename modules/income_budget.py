"""Income-aware budget comparisons, never automatic edits or fixed % rules."""
import re
from config import CATEGORIES


def _mentioned_category(text, plan):
    categories = plan.get("category_monthly") or {}
    limits = plan.get("limits") or {}
    known = sorted(set(categories) | set(limits) | set(CATEGORIES), key=len, reverse=True)
    category = next((name for name in known if re.search(rf"\b{re.escape(name.lower())}\b", text)), None)
    if category:
        return category
    aliases = (
        (r"\bgrocer\w*\b|\bsupermarket\w*\b", "Groceries"),
        (r"\bfood\b|\bdining\b|\btakeaway\w*\b|\brestaurant\w*\b", "Food & Dining"),
        (r"\brent\b", "Housing & Rent"),
        (r"\bfuel\b|\btransportation\b", "Transport"),
    )
    return next((name for pattern, name in aliases if re.search(pattern, text)), None)


def _asks_usual_month(text):
    if re.search(r"\b(?:this|last)\s+month\b", text) and not re.search(
            r"\b(?:average|averag\w*|usual\w*|typical\w*|normally)\b", text):
        return False
    return bool(re.search(
        r"\b(?:average|averag\w*|usual\w*|typical\w*|normally|monthly)\b|"
        r"\b(?:per|each|a)\s+month\b", text))


def _savings_target_for_question(text, plan):
    match = re.search(r"\b(?:save|saving|put aside)\s+(?:about\s+)?\$?(\d[\d,]*(?:\.\d{1,2})?)\b", text)
    if match:
        return float(match.group(1).replace(",", "")), "question"
    return plan.get("monthly_savings_target"), "profile"


def budget_plan(averages, monthly_income=None, monthly_savings_target=None, budgets=None):
    """Compare a user-entered monthly plan with complete statement months.

    A prior month's spending is a baseline, not a recommended limit. We do not
    invent a category percentage: the amount left for a category depends on
    the user's own income, savings target and other observed spending.
    """
    averages = averages or {}
    rows = (budgets or {}).get("budgets") or []
    income = float(monthly_income) if monthly_income is not None and float(monthly_income) > 0 else None
    savings = float(monthly_savings_target) if monthly_savings_target is not None else None
    months = int(averages.get("months_used") or 0)
    monthly = averages.get("monthly") if months else None
    spent = float(monthly["spent"]) if monthly and monthly.get("spent") is not None else None
    categories = {str(k): float(v) for k, v in (averages.get("category_monthly") or {}).items()} if months else {}
    limits = {
        str(row["category"]): {"amount": row.get("suggested_limit"), "source": row.get("limit_source")}
        for row in rows if row.get("category")
    }
    return {
        "monthly_income": income,
        "monthly_savings_target": savings,
        "complete_months": months,
        "usual_monthly_spending": spent,
        "usual_monthly_income_from_statements": float(monthly["income"]) if monthly and monthly.get("income") is not None else None,
        "remaining_before_savings": round(income - spent, 2) if income is not None and spent is not None else None,
        "remaining_after_savings_target": round(income - savings - spent, 2) if income is not None and savings is not None and spent is not None else None,
        "category_monthly": categories,
        "limits": limits,
    }


def monthly_income_question_answer(message, plan, context):
    """Answer observed monthly-income questions without an expense-only tool."""
    text = message.lower()
    if not re.search(r"\b(?:income|earnings|salary|wages|earn)\b", text):
        return None
    if not _asks_usual_month(text):
        return None
    if re.search(r"\b(?:spend\w*|sav\w*|budget\w*|afford|ratio|need to earn)\b", text):
        return None

    currency = context.get("currency") or "AUD"
    months = int(plan.get("complete_months") or 0)
    average = plan.get("usual_monthly_income_from_statements")
    entered = plan.get("monthly_income")
    fact = {"kind": "monthly_income", "complete_months": months,
            "observed_average": average if months else None,
            "entered_monthly_income": entered, "currency": currency}
    if months and average is not None:
        suffix = " This is based on only one complete month, so it may not be typical." if months == 1 else ""
        reply = (f"Across {months} complete calendar month{'s' if months != 1 else ''} in your uploaded statements, "
                 f"income averaged {currency} {average:,.2f} per month. This counts transactions classified "
                 f"as income and excludes transfers.{suffix}")
        fact["read_scope"] = "All uploaded history · complete calendar months"
    else:
        dates = context.get("date_range") or {}
        selected = context.get("income")
        scope = (f" from {dates['start']} to {dates['end']}"
                 if dates.get("start") and dates.get("end") else " in the selected statement period")
        observed = (f" The selected period has {currency} {selected:,.2f} of income{scope}, "
                    "but that is not a monthly average."
                    if isinstance(selected, (int, float)) else "")
        reply = ("I can't calculate a reliable monthly income average yet: your uploaded history has no "
                 f"complete calendar month.{observed} Upload a full month or more to calculate one.")
        fact.update({"read_scope": "Selected statement period · not a monthly average",
                     "date_range": dates, "selected_income": selected})
    if entered is not None:
        reply += (f" You entered {currency} {entered:,.2f} as typical monthly take-home income in Profile; "
                  "that is your planning input, not an amount calculated from the statement.")
    return reply, fact


def monthly_spending_question_answer(message, plan, context):
    """Use complete-month history for usual spending, not the selected slice."""
    text = message.lower()
    if not re.search(r"\b(?:spend\w*|spent|expenses?)\b", text) or not _asks_usual_month(text):
        return None
    if re.search(r"\b(?:budget\w*|afford|ratio|should|recommend|suggest)\b", text):
        return None
    category = _mentioned_category(text, plan)
    months = int(plan.get("complete_months") or 0)
    currency = context.get("currency") or "AUD"
    average = ((plan.get("category_monthly") or {}).get(category, 0.0)
               if category else plan.get("usual_monthly_spending")) if months else None
    fact = {"kind": "monthly_spending", "category": category, "complete_months": months,
            "observed_average": average, "currency": currency}
    label = category or "total spending"
    if months and average is not None:
        reply = (f"Across {months} complete calendar month{'s' if months != 1 else ''} in your uploaded statements, "
                 f"{label} averaged {currency} {average:,.2f} per month. Expenses are net of refunds; "
                 "transfers are excluded.")
        if months == 1:
            reply += " This is based on only one complete month, so it may not be typical."
        fact["read_scope"] = "All uploaded history · complete calendar months"
    else:
        reply = (f"I can't calculate a reliable monthly {label} average yet: your uploaded history "
                 "has no complete calendar month. Upload a full month or more to calculate one.")
        if category is None and isinstance(context.get("spent"), (int, float)):
            selected = context["spent"]
            dates = context.get("date_range") or {}
            scope = (f" from {dates['start']} to {dates['end']}"
                     if dates.get("start") and dates.get("end") else " in the selected period")
            reply += f" The selected period has {currency} {selected:,.2f} of spending{scope}, not a monthly average."
            fact.update({"selected_spending": selected, "date_range": dates})
        fact["read_scope"] = "Selected statement period · not a monthly average"
    return reply, fact


def selected_period_total_answer(message, plan, context):
    """Total income/spending questions use the selected period's known metrics."""
    text = message.lower()
    if re.search(r"\b(?:biggest|largest|top|highest)\b", text):
        return None  # A category ranking is not the overall spending total.
    if _asks_usual_month(text) or _mentioned_category(text, plan):
        return None
    if not re.search(r"\bhow much\b|\btotal\b|\bwhat(?:'s| is| was) my\b", text):
        return None
    # Named merchants need the transaction-search tool instead.
    if re.search(r"\b(?:at|on|from)\s+(?!this\b|last\b|my\b|the\b|all\b)", text):
        return None
    income = bool(re.search(r"\b(?:income|earnings|salary|wages)\b", text))
    spending = bool(re.search(r"\b(?:spend\w*|spent|expenses?)\b", text))
    if income == spending:
        return None
    amount = context.get("income" if income else "spent")
    if not isinstance(amount, (int, float)):
        return None
    dates = context.get("date_range") or {}
    scope = (f" from {dates['start']} to {dates['end']}"
             if dates.get("start") and dates.get("end") else " in the selected statement period")
    label = "income" if income else "spending"
    note = ("Transactions classified as income count; transfers are excluded."
            if income else "Expenses are net of refunds; transfers are excluded.")
    return (f"Your recorded {label} was {context.get('currency') or 'AUD'} {amount:,.2f}{scope}. "
            f"{note} This is the selected period's total, not a monthly average.")


def selected_period_savings_answer(message, context):
    """State observed net savings without implying a partial month is a trend."""
    text = message.lower()
    if not re.search(r"\b(?:how much (?:did|have) i sav\w*|what(?:'s| is| was) my savings rate)\b", text):
        return None
    income, saved = context.get("income"), context.get("saved")
    if not isinstance(income, (int, float)) or not isinstance(saved, (int, float)):
        return "I can't verify net savings for this statement period right now."
    dates = context.get("date_range") or {}
    scope = (f" from {dates['start']} to {dates['end']}"
             if dates.get("start") and dates.get("end") else " in the selected statement period")
    currency = context.get("currency") or "AUD"
    rate = (f" ({saved / income * 100:.1f}% of recorded income)" if income > 0 else "")
    return (f"Your recorded net savings were {currency} {saved:,.2f}{scope}{rate}. "
            "This is income minus spending in the selected period, not your bank balance "
            "or a reliable monthly pace unless the period covers a complete month.")


def budget_overview_answer(message, plan):
    """Answer a broad planning question from complete-month evidence, not guesses."""
    text = message.lower()
    if not re.search(r"\b(?:what should|what would|how should|recommend|suggest)\b.*\b(?:monthly budgets|budgets per month|budgets be)\b", text):
        return None
    income = plan.get("monthly_income")
    if income is None:
        return "Enter your typical monthly take-home income in Profile before I can assess a monthly budget plan. Nothing has changed."
    months = int(plan.get("complete_months") or 0)
    spent = plan.get("usual_monthly_spending")
    if not months or spent is None:
        return "I need at least one complete calendar month of transactions before suggesting evidence-based monthly limits. Nothing has changed."
    savings = plan.get("monthly_savings_target")
    remainder = income - spent - (savings or 0)
    target = (f" and your entered ${savings:,.2f} savings target" if savings is not None
              else " before any savings target")
    direction = (f"leaves ${remainder:,.2f} unallocated" if remainder >= 0 else
                 f"is short by ${-remainder:,.2f}")
    categories = sorted((plan.get("category_monthly") or {}).items(),
                        key=lambda item: (-item[1], item[0]))
    baseline = ", ".join(f"{name} ${amount:,.2f}" for name, amount in categories[:6])
    return (f"Across {months} complete statement month{'s' if months != 1 else ''}, "
            f"you spent about ${spent:,.2f}/month. Against your entered ${income:,.2f} monthly income{target}, "
            f"that {direction}. Historical category averages: {baseline or 'none available'}. "
            "These are baselines, not recommended limits. Tell me which categories to draft, "
            "and review every proposed amount before applying it. Nothing has changed.")


def budget_suggestion_answer(message, plan):
    """Offer a transparent draft reference, never invent or save a target."""
    if not plan:
        return None  # Old callers without planning context keep normal coaching.
    text = message.lower()
    category = _mentioned_category(text, plan)
    if not category or not re.search(
            r"\b(?:should|suggest|recommend|allocate)\b|"
            r"\b(?:need|want)\b.{0,35}\b(?:budget|limit)\b|"
            r"\blooking for\b.{0,35}\b(?:budget|limit)\b", text):
        return None
    if not re.search(r"\b(?:budget|limit|spend)\b", text):
        return None
    if re.search(r"\b(?:set|change|update)\b.{0,50}\bto\s*\$?\d", text):
        return None  # A specific change belongs to the proposal workflow.
    income = plan.get("monthly_income")
    if income is None:
        return (f"I need your typical monthly take-home income to suggest a {category} budget reference. "
                "Add it in Profile; I won't set a limit automatically.")
    months = int(plan.get("complete_months") or 0)
    usual = (plan.get("category_monthly") or {}).get(category)
    total = plan.get("usual_monthly_spending")
    if not months or usual is None or total is None:
        return (f"I can't give an evidence-based {category} starting amount yet because the uploaded history "
                "has no complete-month spending for that category. You can set a limit yourself; "
                "I won't invent one from a fixed percentage.")
    savings, _ = _savings_target_for_question(text, plan)
    other = max(0.0, total - usual)
    room = income - other - (savings or 0.0)
    target = (f" and a ${savings:,.2f} monthly savings target" if savings is not None
              else " before setting a savings target")
    opening = (f"Your {category} spending averaged ${usual:,.2f}/month across {months} complete "
               f"month{'s' if months != 1 else ''}. With ${income:,.2f} entered monthly income{target} "
               f"and about ${other:,.2f} of other usual spending, ")
    if room <= 0:
        return (opening + f"the plan is already short by ${-room:,.2f} before {category}. "
                "Review the whole plan; I can't responsibly suggest a category limit from that shortfall. Nothing changed.")
    if usual > room:
        return (opening + f"only ${room:,.2f} remains for {category}, ${usual - room:,.2f} below your usual amount. "
                "That is a trade-off to review, not a recommendation to cut an essential expense automatically. Nothing changed.")
    return (opening + f"${room:,.2f} remains on paper for {category}. A draft starting reference is your "
            f"${usual:,.2f} historical average, which leaves ${room - usual:,.2f} unallocated. "
            "Choose a limit that fits your actual needs; nothing was saved.")


def budget_plan_fact(message, plan, currency="AUD"):
    """Show the inputs behind budget replies without a misleading period card."""
    category = _mentioned_category(message.lower(), plan)
    savings, source = _savings_target_for_question(message.lower(), plan)
    return {
        "kind": "budget_plan", "read_scope": "Monthly plan · complete statement months",
        "currency": currency, "entered_monthly_income": plan.get("monthly_income"),
        "monthly_savings_target": savings, "savings_target_source": source,
        "complete_months": plan.get("complete_months") or 0,
        "usual_monthly_spending": plan.get("usual_monthly_spending"),
        "category": category,
        "category_monthly": (plan.get("category_monthly") or {}).get(category),
    }


def budget_question_answer(message, plan):
    """Answer budget-amount questions with arithmetic, or ask for missing data."""
    text = message.lower()
    if re.search(r"\b(?:set|change|update|create|raise|lower|reduce|cap)\b", text):
        return None  # Those are proposals, handled by the coach's edit tools.
    if not re.search(r"\b(?:too|high|lot|alot|reasonable|afford|enough|okay|ok)\b|\bis\b", text):
        return None

    categories = plan.get("category_monthly") or {}
    limits = plan.get("limits") or {}
    category = _mentioned_category(text, plan)
    if not category:
        return None

    without_savings_amount = re.sub(
        r"\b(?:save|saving|put aside)\s+(?:about\s+)?\$?\d[\d,]*(?:\.\d{1,2})?\b",
        "", message, flags=re.I)
    quoted = re.search(r"(?<![\w-])\$?(\d[\d,]*(?:\.\d{1,2})?)\b", without_savings_amount)
    amount = float(quoted.group(1).replace(",", "")) if quoted else None
    saved_limit = limits.get(category) or {}
    if amount is None and saved_limit.get("amount") is not None:
        amount = float(saved_limit["amount"])
    if amount is None or amount <= 0:
        return f"I need a monthly {category} amount to compare. Set a limit on Dashboard, or ask about a specific amount."

    source = saved_limit.get("source") if saved_limit.get("amount") is not None and abs(float(saved_limit["amount"]) - amount) < 0.01 else None
    origin = ("your saved limit" if source == "target" else
              "the previous period's spending, not a personalised recommendation" if source == "baseline" else
              "the amount you asked about")
    opening = f"${amount:,.2f} a month for {category} is about ${amount * 12 / 52:,.2f} a week. This figure is {origin}."
    income = plan.get("monthly_income")
    if income is None:
        return (f"{opening} I can't judge whether it fits your budget without your typical monthly take-home income. "
                "Add it in Profile; you can also enter a monthly savings target. I won't change your limit automatically.")

    lines = [opening, f"It is {amount / income * 100:.1f}% of your entered ${income:,.2f} monthly take-home income."]
    months = plan.get("complete_months") or 0
    if months < 1:
        savings, _ = _savings_target_for_question(text, plan)
        room = income - (savings or 0.0)
        if amount > room:
            target = f" after your ${savings:,.2f} savings target" if savings is not None else ""
            lines.append(f"This amount alone exceeds your income{target} by ${amount - room:,.2f}, before any other spending.")
        lines.append("I don't have a complete calendar month of statement history, so I can't compare it with your usual spending or other costs yet.")
        return " ".join(lines)
    usual = categories.get(category)
    if usual is not None:
        lines.append(f"Your {category} spending averaged ${usual:,.2f} across {months} complete month{'s' if months != 1 else ''}.")
    total = plan.get("usual_monthly_spending")
    if total is not None and usual is not None:
        other = max(0.0, total - usual)
        savings, _ = _savings_target_for_question(text, plan)
        room = income - other - (savings or 0.0)
        if savings is not None:
            if room <= 0:
                lines.append(f"Yes, this plan does not balance: about ${other:,.2f} of other usual spending plus your ${savings:,.2f} savings target already exceed income by ${-room:,.2f} before food. Review the whole plan; do not treat a zero food budget as a solution.")
            elif amount > room:
                lines.append(f"Yes, it is too high for the plan you entered if other spending stays similar: with your ${savings:,.2f} monthly savings target and about ${other:,.2f} of other usual spending, it exceeds the remaining room by ${amount - room:,.2f}. The arithmetic ceiling for {category} is ${room:,.2f}; you could instead adjust other limits or your target.")
            else:
                lines.append(f"It fits the plan you entered on paper: after your ${savings:,.2f} savings target and about ${other:,.2f} of other usual spending, it leaves ${room - amount:,.2f} of room. That does not mean you need to spend the full limit.")
        else:
            if amount > room:
                lines.append(f"With about ${other:,.2f} of other usual spending, this amount is ${amount - room:,.2f} above the room left before saving anything.")
            else:
                lines.append(f"With about ${other:,.2f} of other usual spending, it leaves ${room - amount:,.2f} before any savings target.")
    lines.append("That comparison cannot decide what is right for your household. Review the underlying transactions before choosing a rounded limit.")
    return " ".join(lines)
