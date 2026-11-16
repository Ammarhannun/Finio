"""Minimise identifiers sent to AI without changing stored financial records.

Pattern-based redaction reduces exposure; it cannot identify every personal
name or identifier in arbitrary bank text. Amounts and statement dates remain
available for calculations. Reversible placeholders live only for one call.
Credentials are always removed irreversibly.
"""
import re

_SECRETS = re.compile(
    r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|sb_secret_[A-Za-z0-9_-]{10,}"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
    r"|\bBearer\s+[^\s\"',;]+"
    r"|\b(?:password|passcode|otp|verification\s+code)\s*[:=]\s*[^\s\"',;]+",
    re.I,
)
_IDENTIFIERS = [
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"),  # IBAN
    re.compile(r"(?<!\w)(?:\+61[ -]?[23478]|0[23478])(?:[ -]?\d){8}(?!\d)"),
    re.compile(r"(?<!\w)\+\d(?:[ -]?\d){9,14}(?!\d)"),
    re.compile(r"(?<![\w.])(?:\d{4}[ -]){3}\d{4}(?![\w.])"),
    re.compile(r"(?<![\w.$])\d{8,19}(?![\w.])"),
    re.compile(r"\b\d{1,5}\s+[A-Z][A-Z'-]*(?:\s+[A-Z][A-Z'-]*){0,3}\s+"
               r"(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|crescent|cres)\b", re.I),
]
_LABELLED_NUMBER = re.compile(
    r"(\b(?:account(?:\s*(?:number|no\.?))?|acct|a/c|bsb|tfn|card(?:\s*(?:number|no\.?))?)"
    r"\s*[:#=]?\s*)([\dXx*][\dXx* -]{4,26}[\dXx*])\b", re.I)
_PRIVATE_FIELDS = {
    'email', 'phone', 'phonenumber', 'address', 'streetaddress', 'fullname',
    'accountnumber', 'accountid', 'bsb', 'tfn', 'taxfilenumber',
    'accesstoken', 'refreshtoken', 'apikey', 'authorization', 'password',
    'userid', 'revision',
}


def redact_credentials(text):
    return _SECRETS.sub('[REDACTED_SECRET]', str(text))


class AIPrivacy:
    def __init__(self):
        self._originals = {}
        self._tokens = {}

    def _hide(self, original):
        if original not in self._tokens:
            token = f'[FINIO_PRIVATE_{len(self._tokens) + 1}]'
            self._tokens[original] = token
            self._originals[token] = original
        return self._tokens[original]

    def text(self, value):
        value = redact_credentials(value)
        value = _LABELLED_NUMBER.sub(lambda m: m[1] + self._hide(m[2]), value)
        for pattern in _IDENTIFIERS:
            value = pattern.sub(lambda m: self._hide(m[0]), value)
        return value

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(key): self.value(item) for key, item in value.items()
                    if re.sub(r'[^a-z0-9]', '', str(key).lower()) not in _PRIVATE_FIELDS}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value

    def restore(self, value):
        if isinstance(value, str):
            # A single pass prevents a placeholder in bank text from recursively
            # expanding into a different private value.
            return re.sub(r'\[FINIO_PRIVATE_\d+\]', lambda m: self._originals.get(m[0], m[0]), value)
        if isinstance(value, dict):
            return {self.restore(key): self.restore(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.restore(item) for item in value]
        return value


def transaction_sample(rows, limit=15):
    fields = ('date', 'merchant', 'amount', 'category', 'flow', 'is_expense')
    return [{key: row[key][:200] if isinstance(row[key], str) else row[key]
             for key in fields if key in row} for row in (rows or [])[:limit]]
