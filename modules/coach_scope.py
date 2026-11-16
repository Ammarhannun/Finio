"""Explicit read scopes override the selected page window without changing it."""
import calendar
import re


def resolve_coach_scope(message, period=None, month=None, available_months=()):
    text = message.lower()
    if re.search(r"\b(?:all\s+(?:uploaded\s+)?(?:months|history|data|transactions|statements)|all[ -]?time|across\s+(?:my\s+)?(?:entire|whole)\s+history|ever)\b", text):
        return {"period": "all", "month": None, "label": "All uploaded history"}
    explicit = set(re.findall(r"\b(\d{4}-(?:0[1-9]|1[0-2]))\b", text))
    for number in range(1, 13):
        name = calendar.month_name[number].lower()
        short = calendar.month_abbr[number].lower()
        for match in re.finditer(r"\b(?:" + name + "|" + short + r")\b(?:\s+(20\d{2}))?", text):
            year = match.group(1)
            candidates = [f"{year}-{number:02d}"] if year else [m for m in available_months if m.endswith(f"-{number:02d}")]
            if len(candidates) != 1:
                return {"error": "Please include the year for that month so I can use the right statement dates."}
            explicit.update(candidates)
    if len(explicit) > 1:
        return {"error": "Choose All history or ask about one month at a time so I can give you correctly scoped figures."}
    if explicit:
        selected = explicit.pop()
        if selected not in available_months:
            return {"error": f"There are no uploaded transactions for {selected}. Choose a month with uploaded data."}
        return {"period": "monthly", "month": selected, "label": selected}
    return {"period": period, "month": month, "label": month or ("All uploaded history" if period == "all" else "Selected statement period")}
