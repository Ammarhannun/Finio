import json
import os
from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI

from config import COACH_SYSTEM_PROMPT, DISCLAIMER, OPENAI_MODEL

QUICK_QUESTIONS = [
    "How much did I spend?",
    "What's my biggest category?",
    "Am I on track to save?",
]


def has_llm():
    return bool(os.getenv("OPENAI_API_KEY"))


def get_client():
    if not has_llm():
        return None
    return OpenAI()


def build_context(metrics, analysis, bills, budgets=None, invest=None, personality=None):
    saved = metrics["net_saved"]
    breakdown = analysis.get("category_breakdown", [])[:3]
    patterns = [p["message"] for p in analysis.get("patterns", [])]

    context = {
        "currency": "AUD",
        "income": metrics["total_income"],
        "spent": metrics["total_spent"],
        "saved": saved,
        "daily_burn_rate": metrics["daily_burn_rate"],
        "date_range": metrics["date_range"],
        "risk_label": analysis.get("risk_label"),
        "risk_score": analysis.get("risk_score"),
        "top_categories": breakdown,
        "patterns": patterns,
        "bill_count": len(bills),
        "bills_total": round(sum(b["amount"] for b in bills), 2) if bills else 0,
    }

    if budgets:
        context["budgets"] = budgets.get("budgets", [])[:5]
    if invest:
        context["can_invest"] = invest.get("readiness", {}).get("can_invest", False)
        context["invest_readiness_reason"] = invest.get("readiness", {}).get("reason")
        context["etf_recommended"] = invest.get("etf", {}).get("recommended")
    if personality:
        context["personality_type"] = personality.get("personality_type")
        context["savings_rate"] = personality.get("savings_rate")

    return context


def call_llm(system, messages, max_tokens=1024):
    """Single, simple completion (used by the budget/plan/ETF explainers).

    Returns the text, or None if there's no key or the call fails — and records
    the failure reason in `last_llm_error` so a silent fallback is debuggable.
    """
    client = get_client()
    if client is None:
        return None
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}] + messages,
        )
        return response.choices[0].message.content
    except Exception as exc:  # surfaced via last_llm_error, not swallowed
        global last_llm_error
        last_llm_error = f"{type(exc).__name__}: {exc}"
        return None


# ── Tool use ──────────────────────────────────────────────────────────────────
# The coach can call these to compute on the user's real data instead of
# guessing. Each takes the transaction rows / context and returns plain data.

last_llm_error = None
MAX_TOOL_ROUNDS = 4

COACH_TOOLS = [
    {"type": "function", "function": {
        "name": "get_income",
        "description": "Total income for the analysed period, in AUD.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "category_total",
        "description": (
            "Total spent on a category or keyword (e.g. 'Food & Dining', "
            "'coffee', 'uber'). Matches the category name or the merchant text."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }},
    {"type": "function", "function": {
        "name": "filter_transactions",
        "description": (
            "List the user's transactions matching a category or keyword, most "
            "recent first. Use to answer 'what did I buy at X' style questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "max rows (default 10)"},
            },
            "required": ["query"],
        },
    }},
    {"type": "function", "function": {
        "name": "spend_check",
        "description": (
            "Check whether a planned purchase is affordable. Returns a "
            "green/yellow/red verdict and projected balance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "amount": {"type": "number"},
                "days_ahead": {"type": "integer", "description": "horizon, default 30"},
            },
            "required": ["amount"],
        },
    }},
]


def _expense_rows(transactions):
    # is_expense is True only for real expenses (not income/transfers) and is
    # stored in the DB; flow is the in-memory equivalent. Accept either.
    return [t for t in (transactions or [])
            if t.get("is_expense") or t.get("flow") == "expense"]


def _matches(row, query):
    q = (query or "").lower()
    return q in str(row.get("category", "")).lower() or q in str(row.get("merchant", "")).lower()


def run_tool(name, args, transactions, context):
    """Execute one coach tool call and return a JSON-serialisable result."""
    if name == "get_income":
        return {"income": context.get("income"), "currency": "AUD"}

    if name == "category_total":
        query = args.get("query", "")
        rows = [r for r in _expense_rows(transactions) if _matches(r, query)]
        total = round(sum(abs(float(r["amount"])) for r in rows), 2)
        return {"query": query, "total": total, "count": len(rows), "currency": "AUD"}

    if name == "filter_transactions":
        query = args.get("query", "")
        limit = int(args.get("limit") or 10)
        rows = [r for r in _expense_rows(transactions) if _matches(r, query)]
        rows = sorted(rows, key=lambda r: r.get("date", ""), reverse=True)[:limit]
        return {"query": query, "transactions": [
            {"date": r.get("date"), "merchant": r.get("merchant"),
             "amount": round(abs(float(r["amount"])), 2), "category": r.get("category")}
            for r in rows
        ]}

    if name == "spend_check":
        from modules.spend_check import check_purchase
        metrics = {
            "net_saved": context.get("saved", 0),
            "daily_burn_rate": context.get("daily_burn_rate", 0),
            "total_income": context.get("income", 0),
            "total_spent": context.get("spent", 0),
        }
        result = check_purchase(
            None, metrics, float(args["amount"]), int(args.get("days_ahead") or 30)
        )
        result["item"] = args.get("item")
        return result

    return {"error": f"unknown tool {name}"}


def fallback_coach_response(user_message, context, transactions=None):
    """No-API answer that still addresses the question, using the tools' logic."""
    msg = user_message.lower()

    # Try to answer a 'how much on X' question from the actual transactions.
    if transactions and ("how much" in msg or "spend" in msg or "spent" in msg):
        for token in sorted(set(msg.replace("?", " ").split()), key=len, reverse=True):
            if len(token) < 3:
                continue
            res = run_tool("category_total", {"query": token}, transactions, context)
            if res["count"] > 0:
                return (f"You spent ${res['total']:.2f} on '{token}' "
                        f"({res['count']} transactions) this period. {DISCLAIMER}")

    if "biggest" in msg or "category" in msg:
        if context.get("top_categories"):
            top = context["top_categories"][0]
            text = f"Your biggest category is {top['category']} at {top['pct']}% (${top['amount']:.2f})."
        else:
            text = "No spending categories found in this upload."
    elif "spend" in msg or "spent" in msg or "how much" in msg:
        text = (f"You spent ${context['spent']:.2f} this period "
                f"(income ${context['income']:.2f}, saved ${context['saved']:.2f}).")
    elif "track" in msg or "save" in msg or "goal" in msg:
        rate = context.get("savings_rate")
        text = (f"You saved ${context['saved']:.2f} this period"
                + (f" ({rate}% of income)." if rate is not None else "."))
    elif "invest" in msg:
        if context.get("can_invest"):
            etf = context.get("etf_recommended") or "an ETF"
            text = f"You may have savings headroom. Research {etf} and fees before investing."
        else:
            text = context.get("invest_readiness_reason",
                               "Focus on building a cash buffer before investing.")
    else:
        text = " ".join(context.get("patterns", [])[:2]) or \
            "Ask me about your spending, a category (e.g. coffee), saving, or a purchase."

    return f"{text} {DISCLAIMER}"


def coach_chat(user_message, context, history=None, transactions=None):
    """Answer the user's actual question, using tools to query their data.

    Uses OpenAI with function-calling when a key is present; otherwise a
    rule-based fallback that still answers from the transactions.
    """
    global last_llm_error
    last_llm_error = None
    history = history or []
    client = get_client()

    if client is None:
        text = fallback_coach_response(user_message, context, transactions)
        return {"text": text, "source": "fallback", "disclaimer": DISCLAIMER,
                "quick_questions_used": False}

    sample = (transactions or [])[:15]
    system = (
        f"{COACH_SYSTEM_PROMPT}\n\n"
        "You have tools to query the user's real transactions — USE them to get "
        "exact figures rather than estimating. Answer the user's actual question "
        "directly and concisely.\n\n"
        f"Financial summary:\n{json.dumps(context)}\n\n"
        f"Sample of recent transactions:\n{json.dumps(sample)}"
    )
    messages = [{"role": "system", "content": system}] + list(history) + [
        {"role": "user", "content": user_message}
    ]

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=COACH_TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message
            if not msg.tool_calls:
                text = msg.content or ""
                if DISCLAIMER not in text:
                    text = f"{text} {DISCLAIMER}"
                return {"text": text, "source": "openai", "disclaimer": DISCLAIMER}

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = run_tool(tc.function.name, args, transactions, context)
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })
    except Exception as exc:
        last_llm_error = f"{type(exc).__name__}: {exc}"

    # Tool budget exhausted or call failed → still answer the question.
    text = fallback_coach_response(user_message, context, transactions)
    return {"text": text, "source": "fallback", "disclaimer": DISCLAIMER,
            "llm_error": last_llm_error}


def explain_budgets(budgets_result, context):
    system = (
        f"{COACH_SYSTEM_PROMPT}\n\n"
        "Explain these suggested monthly budgets in plain English for a young Australian. "
        "Keep it under 120 words.\n\n"
        f"Context:\n{json.dumps(context)}\n\n"
        f"Budgets:\n{json.dumps(budgets_result.get('budgets', [])[:5])}"
    )
    text = call_llm(system, [{"role": "user", "content": "Explain my budget suggestions."}])
    if text is None:
        lines = ["Suggested monthly limits (with 10% headroom):"]
        for row in budgets_result.get("budgets", [])[:3]:
            if row["actual_spend"] > 0:
                lines.append(
                    f"- {row['category']}: spent ${row['actual_spend']:.2f}, "
                    f"limit ${row['suggested_limit']:.2f}"
                )
        text = "\n".join(lines)
        source = "fallback"
    else:
        source = "openai"
    return {"text": f"{text} {DISCLAIMER}", "source": source, "disclaimer": DISCLAIMER}


def enhance_action_plan(personality_result, context):
    system = (
        f"{COACH_SYSTEM_PROMPT}\n\n"
        "Write a personalised 3-step action plan in plain English. "
        "Use bullet points. Under 100 words.\n\n"
        f"Context:\n{json.dumps(context)}"
    )
    plan = personality_result.get("action_plan", [])
    text = call_llm(
        system,
        [{"role": "user", "content": f"Personality: {personality_result.get('personality_type')}. Improve this plan: {plan}"}],
    )
    if text is None:
        text = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan))
        source = "fallback"
    else:
        source = "openai"
    return {"text": f"{text} {DISCLAIMER}", "source": source, "disclaimer": DISCLAIMER}


def explain_etf_nudge(invest_result, context):
    readiness = invest_result.get("readiness", {})
    if not readiness.get("can_invest"):
        text = readiness.get("reason", "Build your savings buffer before investing.")
        return {"text": f"{text} {DISCLAIMER}", "source": "fallback", "disclaimer": DISCLAIMER}

    etf = invest_result.get("etf", {})
    system = (
        f"{COACH_SYSTEM_PROMPT}\n\n"
        "Explain why this ETF might suit a young Australian investor. "
        "Do not say buy or sell. Under 80 words.\n\n"
        f"Context:\n{json.dumps(context)}\n\n"
        f"ETF info:\n{json.dumps(etf)}"
    )
    text = call_llm(system, [{"role": "user", "content": "Explain this ETF suggestion."}])
    if text is None:
        text = etf.get("reason", "Research fees and risk before investing.")
        source = "fallback"
    else:
        source = "openai"
    return {"text": f"{text} {DISCLAIMER}", "source": source, "disclaimer": DISCLAIMER}


def run_pipeline_context(sample_path=None):
    from config import SAMPLE_CSV
    from modules.pipeline import run_full_pipeline

    result = run_full_pipeline(sample_path or SAMPLE_CSV)
    return {
        "context": result["context"],
        "budgets": result["budgets"],
        "invest": result["invest"],
        "personality": result["personality"],
    }