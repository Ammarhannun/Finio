import os
import tempfile
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from api.deps import AuthUser, get_current_user, get_optional_user
from config import DISCLAIMER
from modules import db
from modules.ai_coach import QUICK_QUESTIONS, coach_chat
from modules.pipeline import recompute_for_goal, run_full_pipeline
from modules.spend_check import check_purchase
from schemas import CoachRequest, GoalRequest, SpendCheckRequest

load_dotenv()

app = FastAPI(title="Finio", description="AI-powered personal finance analyser")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health():
    return {"status": "ok", "app": "Finio", "db_configured": db.is_configured()}


@app.post("/analyze")
async def analyze_csv(
    file: UploadFile = File(...),
    goal_amount: Optional[float] = Form(None),
    goal_date: Optional[str] = Form(None),
    age: Optional[int] = Form(None),
    user: Optional[AuthUser] = Depends(get_optional_user),
):
    filename = (file.filename or "").lower()
    if not (filename.endswith(".csv") or filename.endswith(".pdf")):
        raise HTTPException(status_code=400, detail="Please upload a .csv or .pdf file")

    tmp_path = None
    suffix = ".pdf" if filename.endswith(".pdf") else ".csv"
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            content = await file.read()
            tmp.write(content)

        result = run_full_pipeline(
            tmp_path,
            goal_amount=goal_amount,
            goal_date=goal_date,
            age=age,
        )

        if user:
            try:
                goal_used = result["goal_used"]
                streak = db.persist_analysis(
                    user.token,
                    result,
                    goal_amount=goal_used["amount"],
                    goal_date=goal_used["target_date"],
                    age=goal_used["age"],
                )
                result["streak"] = streak
                result["persisted"] = True
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=500, detail=f"Failed to save to database: {exc}"
                ) from exc
        else:
            result["persisted"] = False

        return result
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def _require_snapshot(user: AuthUser):
    client = db.get_client(user.token)
    data = db.load_dashboard(client, user.user_id)
    if not data or not data.get("metrics"):
        raise HTTPException(
            status_code=404,
            detail="No analysis found — upload a CSV via POST /analyze first",
        )
    return client, data


@app.get("/dashboard")
def dashboard(user: AuthUser = Depends(get_current_user)):
    _, data = _require_snapshot(user)
    data["disclaimer"] = DISCLAIMER
    return data


@app.get("/invest")
def invest(user: AuthUser = Depends(get_current_user)):
    _, data = _require_snapshot(user)
    return {
        "invest": data.get("invest"),
        "forecast": data.get("forecast"),
        "goal": data.get("goal"),
        "metrics": data.get("metrics"),
        "disclaimer": DISCLAIMER,
    }


@app.post("/goal")
def set_goal(body: GoalRequest, user: AuthUser = Depends(get_current_user)):
    client, data = _require_snapshot(user)
    transactions = db.get_transactions(client, user.user_id, data["month"])
    if not transactions:
        raise HTTPException(
            status_code=404, detail="No transactions found — upload a statement first"
        )

    goal_date = body.target_date.isoformat()
    recomputed = recompute_for_goal(
        transactions,
        data["metrics"],
        goal_amount=body.amount,
        goal_date=goal_date,
        age=body.age,
    )
    db.save_goal_recompute(
        client,
        user.user_id,
        data["month"],
        recomputed,
        body.amount,
        goal_date,
        age=body.age,
    )
    return {
        **recomputed,
        "goal": db.get_goal(client, user.user_id),
        "disclaimer": DISCLAIMER,
    }


@app.post("/spend-check")
def spend_check(body: SpendCheckRequest, user: AuthUser = Depends(get_current_user)):
    _, data = _require_snapshot(user)
    metrics = data["metrics"]
    result = check_purchase(None, metrics, body.amount, body.days_ahead)
    result["merchant"] = body.merchant
    return result


@app.get("/coach/history")
def coach_history(user: AuthUser = Depends(get_current_user)):
    client = db.get_client(user.token)
    return {"history": db.get_chat_history(client, user.user_id)}


@app.post("/coach")
def coach(body: CoachRequest, user: AuthUser = Depends(get_current_user)):
    client, data = _require_snapshot(user)
    context = data.get("context")
    if not context:
        raise HTTPException(status_code=404, detail="No coach context — upload a CSV first")

    history_rows = db.get_chat_history(client, user.user_id)
    history = [{"role": row["role"], "content": row["message"]} for row in history_rows]

    response = coach_chat(body.message, context, history=history)

    db.append_chat(client, user.user_id, "user", body.message)
    db.append_chat(client, user.user_id, "assistant", response["text"])

    return {
        **response,
        "quick_questions": QUICK_QUESTIONS,
    }
