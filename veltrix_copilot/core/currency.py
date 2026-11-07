"""Display currencies. All maths is in USD (the policy currency); rates are fixed and indicative."""
from __future__ import annotations

RATES_PER_USD = {"USD": 1.0, "INR": 88.0, "GBP": 0.76, "EUR": 0.87, "AED": 3.67, "SGD": 1.30, "JPY": 150.0}
SYMBOLS = {"USD": "$", "INR": "₹", "GBP": "£", "EUR": "€", "AED": "AED ", "SGD": "S$", "JPY": "¥"}

CITY_CURRENCY = {**dict.fromkeys(["kochi", "bangalore", "bengaluru", "mumbai", "delhi", "new delhi", "chennai",
                                  "trivandrum", "pune"], "INR"),
                 **dict.fromkeys(["paris", "berlin", "amsterdam"], "EUR"), **dict.fromkeys(["new york", "san francisco", "austin"], "USD"),
                 "london": "GBP", "dubai": "AED", "singapore": "SGD", "tokyo": "JPY"}


def rate(code: str) -> float:
    return RATES_PER_USD[code]


def currency_for_city(city: str) -> str:
    return CITY_CURRENCY.get((city or "").strip().lower(), "USD")


def home_currency(home_city: str, configured: str = "") -> str:
    code = (configured or "").strip().upper()
    return code if code in RATES_PER_USD else currency_for_city(home_city)


def convert(usd: float, code: str) -> float:
    return usd * rate(code)


def _indian_grouping(n: int) -> str:
    """1234567 -> 12,34,567 (lakh/crore grouping used for rupees)."""
    s = str(abs(n))
    if len(s) <= 3:
        out = s
    else:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        out = ",".join(groups) + "," + tail
    return ("-" if n < 0 else "") + out


def fmt(amount: float, code: str) -> str:
    """Whole units with the currency's symbol, e.g. £1,020 · ₹1,12,640 · ¥190,000."""
    n = round(amount)
    digits = _indian_grouping(n) if code == "INR" else f"{n:,}"
    return f"{SYMBOLS.get(code, code + ' ')}{digits}"


def fmt_usd_as(usd: float, code: str) -> str:
    return fmt(convert(usd, code), code)
