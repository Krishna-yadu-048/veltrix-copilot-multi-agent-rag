"""Veltrix Travel Policy POL-FIN-001 v2.0 (effective 01 Oct 2025) as numbers the planner can use.
Amounts are in the policy's own currencies; the planner converts them to USD (the search currency).
tests/test_policy_consistency.py checks every number here against data/docs/policies/pol-fin-001-travel-policy.md."""
from __future__ import annotations

import json
import math
from functools import lru_cache

from veltrix_copilot.core import currency
from veltrix_copilot.core.config import ROOT

POLICY_ID = "POL-FIN-001 v2.0"

# 3.3 hotel caps per night and 3.4 daily per diem, by policy region: (amount, currency)
HOTEL_CAP = {"india_tier1": (9000, "INR"), "india_other": (6000, "INR"), "us": (220, "USD"), "eu": (180, "EUR"),
             "sg": (260, "SGD")}
PER_DIEM = {"india_tier1": (4500, "INR"), "india_other": (3000, "INR"), "us": (75, "USD"), "eu": (60, "EUR"),
            "sg": (90, "SGD")}
REGION_LABEL = {"india_tier1": "Indian Tier-1 cities", "india_other": "other Indian cities", "us": "the US",
                "eu": "the EU", "sg": "Singapore"}
FALLBACK_REGION = "us"          # the policy is silent elsewhere (e.g. UK, UAE, Japan): US amounts as a guide
# The policy doesn't list the Tier-1 cities; this is the usual Indian Tier-1 list.
INDIA_TIER1 = {"Bangalore", "Mumbai", "Delhi", "Chennai", "Hyderabad", "Kolkata", "Pune", "Ahmedabad"}
EU_COUNTRIES = {"Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czechia", "Denmark", "Estonia", "Finland",
                "France", "Germany", "Greece", "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg",
                "Malta", "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia", "Spain", "Sweden"}

DOMESTIC_NOTICE_DAYS = 7              # 3.1 book at least 7 days ahead for domestic trips...
INTERNATIONAL_NOTICE_DAYS = 21        # ...and 21 days for international trips
PREMIUM_ECONOMY_MIN_HOURS = 6         # 3.2 international flights longer than 6 hours may be premium economy
DEPT_HEAD_APPROVAL_OVER_USD = 5000    # 3.6 trips above $5,000 need department head approval

# Planner settings (not in the policy): how flights are compared and when the return may leave.
MAX_STOPS = 2
MIN_LAYOVER_MINUTES = 60
MAX_LAYOVER_MINUTES = 480
STOP_COST_USD = 75            # when comparing fares, each stop counts as this much extra...
HOUR_COST_USD = 20            # ...and each hour of total travel time as this much
MIN_GROUND_HOURS = 12         # the return flight leaves at least this long after landing
EARLIEST_RETURN_HOUR = 6      # ...and not in the small hours of the return day


@lru_cache(maxsize=1)
def _cities() -> dict:
    return json.loads((ROOT / "mcp_servers" / "data" / "cities.json").read_text(encoding="utf-8"))


def country(city: str) -> str:
    return _cities().get((city or "").strip().lower(), {}).get("country", "")


def region(city: str) -> str | None:
    """The policy region of a city, or None when the policy doesn't cover its country."""
    c = _cities().get((city or "").strip().lower(), {})
    land = c.get("country", "")
    if land == "India":
        return "india_tier1" if c.get("name") in INDIA_TIER1 else "india_other"
    return {"United States": "us", "Singapore": "sg"}.get(land) or ("eu" if land in EU_COUNTRIES else None)


def is_domestic(origin: str, destination: str) -> bool:
    return bool(country(origin)) and country(origin) == country(destination)


def _usd(amount: float, code: str) -> int:
    return math.floor(amount / currency.rate(code))      # never round a cap up


def limits(city: str) -> dict:
    """Hotel cap and per diem for a city, in the policy currency and in USD."""
    reg = region(city)
    use = reg or FALLBACK_REGION
    (cap, cap_cur), (pd, pd_cur) = HOTEL_CAP[use], PER_DIEM[use]
    return {"region": use, "covered": reg is not None, "label": REGION_LABEL[use],
            "hotel_cap": currency.fmt(cap, cap_cur), "per_diem": currency.fmt(pd, pd_cur),
            "hotel_cap_usd": _usd(cap, cap_cur), "per_diem_usd": _usd(pd, pd_cur)}


def hotel_cap(city: str) -> int:
    return limits(city)["hotel_cap_usd"]


def per_diem(city: str) -> int:
    return limits(city)["per_diem_usd"]
