"""Shared transfer-review population for totals and merchant questions."""
import pandas as pd
from modules.data_processor import _override_mask


def unresolved_transfers(frame, overrides=None):
    reviewed = pd.Series(False, index=frame.index)
    for rule in overrides or []:
        if rule.get('flow') == 'transfer':
            reviewed |= _override_mask(frame, rule)
    return frame[(frame['flow'] == 'transfer') & ~reviewed]


def transfer_review(frame, overrides=None):
    rows = unresolved_transfers(frame, overrides)
    column = 'merchant_clean' if 'merchant_clean' in rows else 'description'
    return {'count': len(rows), 'merchant_count': int(rows[column].astype(str).str.strip().str.upper().nunique()),
            'money_out': round(float(-rows.loc[rows.amount < 0, 'amount'].sum()), 2),
            'money_in': round(float(rows.loc[rows.amount > 0, 'amount'].sum()), 2)}
