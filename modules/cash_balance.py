"""Dated available cash, separate from statement income minus spending."""
from datetime import date
import math

MONTH_DAYS = 30.44


def _number(value):
    try:
        number = float(value)
        return number if value is not None and math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def balance_view(data, today=None):
    today = today or date.today()
    saved = data.get('cash_balance') or {}
    metrics = data.get('metrics') or {}
    statement = data.get('statement_balance') or {}
    candidates = [
        {'amount': _number(saved.get('amount')), 'as_of': saved.get('as_of'), 'source': 'saved', 'priority': 2},
        {'amount': _number(statement.get('amount')), 'as_of': statement.get('as_of'), 'source': 'statement', 'priority': 1},
        {'amount': _number(metrics.get('latest_balance')), 'as_of': metrics.get('balance_date'), 'source': 'statement', 'priority': 0},
    ]
    def rank(point):
        try:
            day = date.fromisoformat(point['as_of'])
            valid = day <= today
        except (TypeError, ValueError):
            day, valid = date.min, False
        return valid, day if valid else date.min, point['priority']
    point = max((p for p in candidates if p['amount'] is not None), key=rank, default={})
    amount, as_of, source = point.get('amount'), point.get('as_of'), point.get('source')
    try:
        age = (today - date.fromisoformat(as_of)).days
    except (TypeError, ValueError):
        age = None
    current = amount is not None and age == 0
    averages = data.get('averages') or {}
    months = averages.get('months_used') or 0
    monthly = _number((averages.get('monthly') or {}).get('spent')) if months else None
    monthly = monthly if monthly is not None and monthly > 0 else None
    coverage = amount / monthly if amount is not None and monthly else None
    pending = int((data.get('history_review') or data.get('review') or {}).get('count') or 0)

    if amount is None:
        status, label, tone = 'missing', 'Add your balance', 'neutral'
        message = 'No balance was saved from this import. Add your available bank balance, or upload a statement with a balance column.'
    elif not current:
        status, label, tone = 'stale', 'Update needed', 'neutral'
        message = 'This is a last known balance. Confirm today’s available balance before a purchase check.'
    elif amount <= 0:
        status, label, tone = 'low', 'Low balance', 'red'
        message = 'There is no positive available cash in this balance.'
    elif pending:
        status, label, tone = 'review', 'Review needed', 'neutral'
        message = 'Review unconfirmed transfers to make the spending comparison more reliable.'
    elif monthly is None:
        status, label, tone = 'unknown', 'More history needed', 'neutral'
        message = 'A full month of positive recorded spending is needed for a monthly comparison.'
    elif coverage >= 1:
        status, label, tone = 'comfortable', 'Comfortable', 'green'
        message = 'Covers at least one usual month of recorded spending before future income.'
    elif amount >= monthly / MONTH_DAYS * 7:
        status, label, tone = 'tight', 'Getting tight', 'yellow'
        message = 'Covers at least a week, but less than a usual month of recorded spending.'
    else:
        status, label, tone = 'low', 'Low balance', 'red'
        message = 'Covers less than a week of usual recorded spending.'
    return {'amount': amount, 'as_of': as_of, 'source': source if amount is not None else None,
            'is_current': current, 'today': today.isoformat(), 'status': status,
            'label': label, 'tone': tone, 'message': message, 'monthly_spending': monthly,
            'months_used': months, 'months_covered': round(coverage, 2) if coverage is not None else None,
            'days_covered': round(max(0, coverage) * MONTH_DAYS, 1) if coverage is not None else None,
            'provisional': bool(pending), 'transfer_review_count': pending}


def spending_metrics(metrics, data):
    """Use the same dated cash and full-month baseline on every purchase surface."""
    result = dict(metrics)
    view = balance_view(data)
    result.update(latest_balance=view['amount'], balance_date=view['as_of'],
                  user_entered_balance=view['source'] == 'saved', balance_source=view['source'],
                  transfer_review_count=int((data.get('history_review') or data.get('review') or {}).get('count') or 0))
    if view['monthly_spending'] is not None:
        result['daily_burn_rate'] = view['monthly_spending'] / MONTH_DAYS
        result['has_spending_baseline'] = True
    else:
        # A partial statement is not enough to promise a month of affordability.
        result['has_spending_baseline'] = False
    return result
