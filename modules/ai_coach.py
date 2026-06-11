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
    except Exception:
        return None


def fallback_coach_response(user_message, context):
    msg = user_message.lower()

    if "spend" in msg or "spent" in msg:
        text = (
            f"You spent ${context['spent']:.2f} in this period "
            f"(income ${context['income']:.2f}, saved ${context['saved']:.2f})."
        )
    elif "biggest" in msg or "category" in msg:
        if context["top_categories"]:
            top = context["top_categories"][0]
            text = (
                f"Your biggest category is {top['category']} "
                f"at {top['pct']}% (${top['amount']:.2f})."
            )
        else:
            text = "No spending categories found in this upload."
    elif "track" in msg or "save" in msg or "goal" in msg:
        text = (
            f"You saved ${context['saved']:.2f} this period "
            f"({context['savings_rate']}% of income)."
            if context.get("savings_rate") is not None
            else f"You saved ${context['saved']:.2f} this period."
        )
    elif "invest" in msg:
        if context.get("can_invest"):
            etf = context.get("etf_recommended") or "an ETF"
            text = (
                f"You may have savings headroom. Research {etf} and fees before investing. "
                f"{context.get('invest_readiness_reason', '')}"
            )
        else:
            text = context.get(
                "invest_readiness_reason",
                "Focus on building a cash buffer before investing.",
            )
    else:
        pattern_text = " ".join(context.get("patterns", [])[:2])
        text = pattern_text or "Upload a CSV and ask about spending, saving, or budgets."

    return f"{text} {DISCLAIMER}"


def coach_chat(user_message, context, history=None):
    history = history or []
    system = f"{COACH_SYSTEM_PROMPT}\n\nFinancial context:\n{json.dumps(context)}"
    messages = list(history) + [{"role": "user", "content": user_message}]

    text = call_llm(system, messages)
    source = "openai" if text else "fallback"
    if text is None:
        text = fallback_coach_response(user_message, context)
    elif DISCLAIMER not in text:
        text = f"{text} {DISCLAIMER}"

    return {"text": text, "source": source, "disclaimer": DISCLAIMER}


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