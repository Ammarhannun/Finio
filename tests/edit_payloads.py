"""Export real Python edit payloads for the isolated PostgreSQL contract test."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from modules import db
from modules.pipeline import analyze_stored

ROWS = [
    {'date':'2026-06-01','merchant':'SALARY','amount':2000,'flow':'income','category':'Income'},
    {'date':'2026-06-02','merchant':'COLES','amount':-110,'flow':'expense','category':'Groceries'},
    {'date':'2026-06-03','merchant':'COLES REFUND','amount':10,'flow':'expense','category':'Groceries'},
    {'date':'2026-06-04','merchant':'PAYID ALEX','amount':-25,'flow':'transfer','category':'Transfers'},
]


def payloads():
    original = analyze_stored(ROWS, goal_amount=6000, goal_date='2027-12-01')
    snapshot = {'summary_json': deepcopy(original), 'month':'2026-06-01'}
    exports = []

    class Client:
        def rpc(self, name, params):
            assert name == 'finio_edit_analysis'
            exports.append(params)
            return SimpleNamespace(execute=lambda: SimpleNamespace(data={}))

    with patch.object(db, 'get_latest_snapshot', return_value=snapshot), patch.object(db, 'get_all_transactions', return_value=ROWS):
        for kind in ('budget', 'goal', 'profile', 'transfer', 'balance', 'statement_balance'):
            options = {'goal_amount':6000, 'goal_date':'2027-12-01'}
            changes, profile = {}, {}
            if kind == 'budget':
                options['budget_targets'] = {'Groceries':350}
                changes = {'budget_targets':options['budget_targets']}
            elif kind == 'goal':
                options['goal_amount'] = 7000
            elif kind == 'profile':
                options['age'] = 35
                profile = {'age':35}
            elif kind == 'balance':
                changes = {'cash_balance': {'amount': 1234.56, 'as_of': '2026-06-30'}}
            elif kind == 'statement_balance':
                changes = {'statement_balance': {'amount': 1080, 'as_of': '2026-06-11'}}
            else:
                options['overrides'] = [{'match':'PAYID ALEX','flow':'expense','category':'Other'}]
                changes = {'overrides':options['overrides']}
            db.commit_edit(Client(), 'synthetic', None, resliced=analyze_stored(ROWS, **options),
                           changes=changes, profile=profile, label=kind)
    return exports


if __name__ == '__main__':
    print(json.dumps(payloads()))
