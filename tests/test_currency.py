"""Issue: prices didn't match the country (everything was shown in USD only)."""
import pytest

from veltrix_copilot.core import currency
from veltrix_copilot.agents.travel_agent import budget_line, parse_budget, plan_summary_text, render_cost_table, sanitize_itinerary


def test_indian_grouping_and_symbols():
    assert currency.fmt(112640, "INR") == "₹1,12,640"
    assert currency.fmt(1020.4, "GBP") == "£1,020"
    assert currency.fmt(950, "INR") == "₹950"
    assert currency.fmt(190000, "JPY") == "¥190,000"


def test_city_and_home_currency():
    assert currency.currency_for_city("London") == "GBP"
    assert currency.currency_for_city("Paris") == "EUR"
    assert currency.home_currency("Kochi") == "INR"
    assert currency.home_currency("Kochi", "gbp") == "GBP"   # explicit HOME_CURRENCY wins


@pytest.mark.parametrize("text,expected", [
    ("budget of $1500", (1500.0, "USD")),
    ("under ₹1,20,000", (120000.0, "INR")),
    ("around 90000 rupees", (90000.0, "INR")),
    ("rs. 75,000 max", (75000.0, "INR")),
    ("with £900", (900.0, "GBP")),
    ("for 1,500 euros", (1500.0, "EUR")),
    ("budget of 2000", (2000.0, "USD")),
    ("a 2-day trip", None),
])
def test_budget_currency_parsing(text, expected):
    assert parse_budget(text) == expected


PLAN = {
    "origin": "Kochi", "destination": "London", "start": "2026-11-02", "end": "2026-11-03", "days": 2, "nights": 1,
    "outbound": {"flight_no": "AI317", "airline": "Air India", "depart": "2026-11-02T02:35", "arrive": "2026-11-02T15:05", "price_usd": 470},
    "return": {"flight_no": "AI318", "airline": "Air India", "depart": "2026-11-03T02:35", "arrive": "2026-11-03T15:05", "price_usd": 480},
    "hotel": {"name": "Bloomsbury Court Hotel", "area": "Bloomsbury", "price_per_night": 172},
    "costs": {"flights": 950, "hotel": 172, "per_diem": 150, "total": 1272},
    "per_diem_rate": 75, "hotel_cap": 180,
    "home_currency": "INR", "local_currency": "GBP",
}


def test_cost_table_shows_home_currency_column():
    table = render_cost_table(PLAN)
    assert "| Item | Details | USD | ≈ INR |" in table
    assert f"**{currency.fmt_usd_as(1272, 'INR')}**" in table


def test_budget_given_in_rupees_is_reported_in_rupees():
    plan = {**PLAN, "budget": 120000 / currency.rate("INR"), "budget_amount": 120000, "budget_currency": "INR"}
    line = budget_line(plan)
    assert "₹1,20,000" in line and "Within" in line


def test_email_summary_has_both_currencies():
    text = plan_summary_text(PLAN)
    assert "Estimated total: $1,272 (≈ ₹" in text


@pytest.mark.parametrize("line", [
    "Day 1: Dinner costs about 40 pounds.",
    "Day 2: Budget around Rs. 3000 for meals.",
    "Day 2: Expect to spend 50 euros.",
    "Day 1: Taxi fare is ¥3000.",
])
def test_itinerary_money_in_any_currency_is_removed(line):
    assert sanitize_itinerary(f"Day 1: Arrive.\n{line}") == "Day 1: Arrive."
