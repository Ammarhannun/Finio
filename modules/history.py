import json
from datetime import date, datetime
from pathlib import Path

from config import DISCLAIMER, DATA_DIR

SNAPSHOTS_DIR = DATA_DIR / "snapshots"
STREAK_WINDOW_DAYS = 7


def build_snapshot(df, metrics, analysis, bills, month=None):
    if month is None:
        month = str(df["month"].iloc[-1])

    breakdown = analysis.get("category_breakdown", [])
    top_category = breakdown[0] if breakdown else None

    return {
        "month": month,
        "saved_at": datetime.now().isoformat(),
        "metrics": metrics,
        "risk_score": analysis["risk_score"],
        "risk_label": analysis["risk_label"],
        "top_category": top_category,
        "bill_count": len(bills),
        "patterns": analysis["patterns"],
        "disclaimer": DISCLAIMER,
    }


def update_streak(last_upload, today=None, current_streak=0, best_streak=0):
    today = today or date.today()
    if last_upload is None:
        return {
            "current_streak": 1,
            "best_streak": max(1, best_streak),
            "last_upload": today.isoformat() if isinstance(today, date) else today,
        }

    if isinstance(last_upload, str):
        last_upload = date.fromisoformat(last_upload)
    if isinstance(today, str):
        today = date.fromisoformat(today)

    days_gap = (today - last_upload).days
    current = current_streak + 1 if days_gap <= STREAK_WINDOW_DAYS else 1

    return {
        "current_streak": current,
        "best_streak": max(best_streak, current),
        "last_upload": today.isoformat(),
    }


def save_snapshot(snapshot, filename=None):
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = f"snapshot_{snapshot['month']}.json"
    path = SNAPSHOTS_DIR / filename
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
    return path


def load_snapshots():
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SNAPSHOTS_DIR.glob("snapshot_*.json"))
    return [json.loads(path.read_text()) for path in files]
