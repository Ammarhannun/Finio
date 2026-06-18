"""Finio constants — categories, keywords, colours, disclaimers."""

from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
SAMPLE_CSV = DATA_DIR / "sample_transactions.csv"
TRAINING_CSV = DATA_DIR / "training_merchants.csv"

# Normalized transaction schema (after parsing any bank CSV)
NORMALIZED_COLUMNS = ["date", "amount", "description", "balance"]

# Common export headers — banks use different names; parser maps these → normalized
# Matching is case-insensitive (see bank_parser._find_column)
COMMON_DATE_HEADERS = ["Date", "Transaction Date", "Posted Date", "Date of Transaction"]
COMMON_AMOUNT_HEADERS = ["Amount", "Value", "Transaction Amount"]
COMMON_DEBIT_HEADERS = ["Debit", "Debit Amount", "Withdrawal", "Withdrawals", "Money Out"]
COMMON_CREDIT_HEADERS = ["Credit", "Credit Amount", "Deposit", "Deposits", "Money In"]
COMMON_DESCRIPTION_HEADERS = [
    "Description", "Narrative", "Details", "Memo", "Transaction Details", "Reference"
]
COMMON_BALANCE_HEADERS = ["Balance", "Running Balance", "Account Balance"]
COMMON_TYPE_HEADERS = ["Type", "Transaction Type", "Debit/Credit", "Dr/Cr"]

# Date formats tried in order; AU is day-first. Falls back to pandas inference.
DATE_FORMAT = "%d/%m/%Y"  # kept for backward compatibility
DATE_FORMATS = [
    "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%m/%d/%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%b %d %Y",  # PDF month-name dates
]

# ML categories (categoriser.py) — 7 general buckets
CATEGORIES = [
    "Food & Dining",   # restaurants, delivery, takeaway, cafes
    "Groceries",       # supermarkets, convenience stores
    "Transport",       # opal, uber trip, fuel, parking
    "Subscriptions",   # streaming, gym, phone plans
    "Shopping",        # retail, amazon, electronics, clothes
    "Health",          # pharmacy, medical
    "Other",           # rent, uncategorised
]

# "Transfers" is NOT a spend bucket. Money moved between your own accounts or to
# people (P2P) is internal, not consumption — it is excluded from total_spent,
# total_income, and the spending breakdown. Kept separate so it never inflates
# the numbers or the "Other" pile.
TRANSFERS_LABEL = "Transfers"
TRANSFER_KEYWORDS = [
    "TRANSFER TO", "TRANSFER FROM", "TFR TO", "TFR FROM",
    "PAYID", "OSKO", "PAY ANYONE", "INTERNAL TRANSFER", "INTER-ACCOUNT",
]

# --- Merchant-name cleaning (categoriser input) ---------------------------
# Card-network / payment-processor prefixes banks bolt onto the front.
MERCHANT_PREFIXES = ["SQ ", "SQ*", "SP ", "SP*", "PAYPAL ", "PAYPAL*", "PP ", "TPV ", "EFTPOS "]
# Corporate/legal suffix noise that buries the real trading name.
MERCHANT_NOISE = [
    " PTY LTD", " PTY LT", " PTY", " LTD", " LIMITED", " INC", " CO",
    " HOLDINGS", " ENTERPRISE", " ENTERPRISES",
]
# Trailing country/state tokens CommBank appends (stripped when at the end).
MERCHANT_TRAILING = ["AUS", "AU", "NSWAU", "NSW", "NS", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"]
# Suburb / locality stop-words — location noise, never part of the merchant.
SUBURB_STOPWORDS = {
    "SYDNEY", "RYDE", "NORTH", "AUBURN", "GRANVILLE", "KINGSFORD", "MEADOWBANK",
    "ROZELLE", "LIDCOMBE", "KELLYVILLE", "BONDI", "JUNCTION", "NEWTOWN", "SURRY",
    "HILLS", "BROADWAY", "CBD", "PARRAMATTA", "CHATSWOOD", "BURWOOD", "STRATHFIELD",
    "EASTWOOD", "EPPING", "MARSFIELD", "MACQUARIE", "HORNSBY", "CASTLE", "HILL",
    "LANE", "COVE", "MANLY", "RANDWICK", "KENSINGTON", "MASCOT", "REDFERN",
    "ULTIMO", "HAYMARKET", "GLEBE", "LEICHHARDT", "ASHFIELD", "BANKSTOWN",
}

# --- Keyword rules layer (PRIMARY categoriser) ----------------------------
# Ordered list of (keywords, category). First keyword found in the cleaned
# merchant name wins, so put specific entries before general ones
# (e.g. "UBER EATS" before "UBER", "AMAZON PRIME" before "AMAZON").
CATEGORY_RULES = [
    ("Food & Dining", [
        "UBER EATS", "UBEREATS", "DOORDASH", "DELIVEROO", "MENULOG", "EASI",
        "MCDONALD", "KFC", "HUNGRY JACK", "NANDOS", "GUZMAN", "ZAMBRERO",
        "SUBWAY", "DOMINO", "PIZZA", "BURGER", "SHAWARMA", "KEBAB", "CHICKEN",
        "SUSHI", "RAMEN", "NOODLE", "THAI", "INDIAN", "PAKWAAN", "CURRY",
        "GRILL", "BBQ", "CAFE", "COFFEE", "STARBUCKS", "GLORIA JEAN", "BAKERY",
        "DONUT", "DESSERT", "GELATO", "RESTAURANT", "BISTRO", "DINER", "EATERY",
        "KITCHEN", "FOOD", "CHIPOTLE", "BOOST JUICE", "CHATIME", "GONG CHA",
    ]),
    ("Groceries", [
        "WOOLWORTHS", "WOOLIES", "COLES", "ALDI", "IGA", "COSTCO", "7-ELEVEN",
        "7 ELEVEN", "SEVEN ELEVEN", "FOODWORKS", "HARRIS FARM", "SUPERMARKET",
        "GROCER", "BUTCHER", "FRUIT", "SUPER", "MARKET", "DELI",
    ]),
    ("Transport", [
        "UBER", "DIDI", "OLA", "OPAL", "TRANSPORT NSW", "TRANSPORTFORNSW",
        "GO VIA", "LINKT", "E-TOLL", "ETOLL", "TOLL", "BP ", "SHELL", "CALTEX",
        "AMPOL", "7-ELEVEN FUEL", "COLES EXPRESS", "FUEL", "PETROL", "PARKING",
        "WILSON PARKING", "SECURE PARKING", "CARPARK", "TRAINLINK", "METRO",
        "TAXI", "13CABS",
    ]),
    ("Subscriptions", [
        "NETFLIX", "SPOTIFY", "DISNEY", "STAN", "BINGE", "PARAMOUNT", "PRIME VIDEO",
        "AMAZON PRIME", "YOUTUBE PREMIUM", "APPLE.COM", "ITUNES", "GOOGLE STORAGE",
        "ADOBE", "MICROSOFT", "DROPBOX", "AUDIBLE", "PATREON", "CHATGPT", "OPENAI",
        "TELSTRA", "OPTUS", "VODAFONE", "AMAYSIM", "BELONG", "GYM", "FITNESS",
        "ANYTIME FITNESS", "F45", "PLUS FITNESS", "GYMSHARK",
    ]),
    ("Shopping", [
        "AMAZON", "EBAY", "KMART", "TARGET", "BIG W", "JB HI-FI", "JBHIFI",
        "JB HIFI", "HARVEY NORMAN", "THE GOOD GUYS", "OFFICEWORKS", "BUNNINGS",
        "IKEA", "MYER", "DAVID JONES", "UNIQLO", "COTTON ON", "H&M", "ZARA",
        "MECCA", "SEPHORA", "REBEL", "SUPERCHEAP", "CHEMIST WAREHOUSE ONLINE",
        "THE ICONIC", "ASOS", "TEMU", "SHEIN", "ALIEXPRESS",
    ]),
    ("Health", [
        "PHARMACY", "CHEMIST", "PRICELINE", "TERRY WHITE", "MEDICARE", "MEDICAL",
        "DENTAL", "DENTIST", "DOCTOR", "GP ", "CLINIC", "HOSPITAL", "PHYSIO",
        "OPTICAL", "OPTOMETRIST", "PATHOLOGY", "RADIOLOGY", "HEALTH",
    ]),
]

# Australian context
CURRENCY = "AUD"
DISCLAIMER = "General information only, not financial advice"

# Savings-rate guard: below this much real (non-transfer) income, a percentage
# is meaningless (e.g. a statement funded entirely by transfers), so the single
# source of truth reports savings_rate = None instead of a wild number.
MIN_INCOME_FOR_RATE = 100.0

# Flow types — the canonical classification every transaction carries.
FLOW_INCOME = "income"
FLOW_EXPENSE = "expense"
FLOW_TRANSFER = "transfer"

# Time-period selector (period.py). The pipeline filters to one period at the
# top so totals, burn rate, patterns, budgets and charts all describe the same
# window. Default is the latest calendar month in the data.
DEFAULT_PERIOD = "monthly"
PERIODS_SUPPORTED = ["daily", "weekly", "monthly", "custom", "all"]

# ASX ETFs mentioned in the plan
ETF_OPTIONS = ["VGS", "A200", "NDQ"]

# Beyond ETFs: the invest page shows a fuller menu so users see the trade offs
# between cash, shares, crypto and super rather than only index funds. Crypto is
# included for completeness with a heavy risk caveat, never as a recommendation.
CRYPTO_OPTIONS = ["BTC", "ETH"]

# ai_coach.py — OpenAI (optional; rule-based fallbacks if no key)
OPENAI_MODEL = "gpt-4o-mini"
COACH_SYSTEM_PROMPT = (
    "You are Finio, a warm, upbeat money companion for young Australians aged 18 to 30. "
    "Chat naturally and answer whatever the user asks, including everyday questions, "
    "not just preset finance ones. When a question touches their money, ground your "
    "answer in the financial context and use your tools to pull exact figures rather "
    "than guessing. Currency is AUD. Keep replies friendly and concise. "
    "Give general information only, never personal financial advice, and never tell "
    "the user to buy or sell a specific investment. "
    "If can_invest is false, steer toward saving and building a buffer instead of investing. "
    "Never use hyphens in your replies."
)

# bill_detector.py — a real recurring bill must pass ALL three tests below.
# 1. Occurs often enough to be a pattern, not a one-off.
BILL_MIN_OCCURRENCES = 3
# 2. Amount is stable: coefficient of variation (std/mean) under this. Keeps out
#    variable spend like restaurants/groceries that recur at the same merchant.
BILL_AMOUNT_CV_MAX = 0.25
# 3. Timing is regular: gaps cluster near a known billing period. We match the
#    median gap to the nearest period within BILL_PERIOD_TOLERANCE (relative),
#    and require the gaps themselves to be consistent (std/median under cap).
BILL_PERIODS = {"weekly": 7, "fortnightly": 14, "monthly": 30, "quarterly": 91}
BILL_PERIOD_TOLERANCE = 0.35
# Real bills jitter and occasionally skip/retry, so a gap of ~2x or ~3x the
# period is still "on schedule". We accept a merchant when most gaps fall near
# an integer multiple of the period (within BILL_PERIOD_TOLERANCE).
BILL_REGULARITY_MIN_FRACTION = 0.7
