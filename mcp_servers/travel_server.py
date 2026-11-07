"""Travel MCP server: live flights and hotels from Google Flights / Google Hotels via SerpAPI.

Needs SERPAPI_API_KEY in .env. Cost guards (monthly cap, reserve, 24 h cache) are in serpapi_client.py;
when a guard trips or nothing is found, the tool returns a clear error instead of made-up data."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_servers.common import data_dir, env, make_server
from mcp_servers.serpapi_client import SerpApiClient, SerpApiError, parse_flights, parse_hotels

# Airport code and time zone per city (SerpAPI searches by airport code; times are local to each airport).
CITIES: dict = json.loads((Path(__file__).resolve().parent / "data" / "cities.json").read_text(encoding="utf-8"))

mcp, PORT = make_server("travel", 8101)
_serp: SerpApiClient | None = None


def serp() -> SerpApiClient:
    global _serp
    if _serp is None:
        _serp = SerpApiClient(
            api_key=env("SERPAPI_API_KEY", ""),
            cache_dir=data_dir() / "cache" / "serpapi",
            usage_file=data_dir() / "serpapi_usage.json",
            monthly_limit=int(env("SERPAPI_MONTHLY_LIMIT", "100")),
            min_searches_left=int(env("SERPAPI_MIN_SEARCHES_LEFT", "20")),
            cache_hours=float(env("SERPAPI_CACHE_HOURS", "24")),
        )
    return _serp


def _search(params: dict) -> dict:
    try:
        return serp().search({**params, "currency": "USD", "hl": "en", "gl": env("SERPAPI_GL", "in")})
    except SerpApiError as e:
        raise ValueError(f"Live travel search failed: {e}") from None


def _city(name: str) -> dict:
    key = (name or "").strip().lower()
    if key not in CITIES:
        raise ValueError(f"Unknown city '{name}'. Supported cities: {', '.join(sorted({c['name'] for c in CITIES.values()}))}")
    return CITIES[key]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ValueError("date must be YYYY-MM-DD") from e


@mcp.tool()
def search_flights(origin: str, destination: str, date: str) -> list[dict]:
    """Search one-way economy flights between two cities on a date.

    Args:
        origin: departure city name, e.g. "Kochi"
        destination: arrival city name, e.g. "London"
        date: departure date, YYYY-MM-DD
    Returns flights sorted by price (USD). `depart`/`arrive` are local times at each airport
    (time zones in `depart_tz`/`arrive_tz`).
    """
    o, d = _city(origin), _city(destination)
    if o["code"] == d["code"]:
        raise ValueError("origin and destination are the same city")
    day = _parse_date(date)
    data = _search({"engine": "google_flights", "departure_id": o["code"], "arrival_id": d["code"],
                    "outbound_date": day.isoformat(), "type": "2", "adults": "1", "travel_class": "1"})
    if not (flights := parse_flights(data, o, d)):
        raise ValueError(f"No live flights found from {o['name']} to {d['name']} on {day.isoformat()}")
    return flights


@mcp.tool()
def search_hotels(city: str, check_in: str, nights: int, max_price_per_night: float) -> list[dict]:
    """Search hotels in a city at or under a nightly price cap (USD).

    Args:
        city: city name, e.g. "London"
        check_in: YYYY-MM-DD
        nights: number of nights (1-30)
        max_price_per_night: maximum nightly rate in USD
    Returns real hotels only (3+ stars, rated 3.5+, enough reviews), best first, then price.
    """
    c, day, nights = _city(city), _parse_date(check_in), int(nights)
    if not 1 <= nights <= 30:
        raise ValueError("nights must be between 1 and 30")
    data = _search({"engine": "google_hotels", "q": f"hotels in {c['name']}", "check_in_date": day.isoformat(),
                    "check_out_date": (day + timedelta(days=nights)).isoformat(), "adults": "1", "sort_by": "8",
                    "max_price": str(int(max_price_per_night)), "hotel_class": "3,4,5", "rating": "7"})
    return parse_hotels(data, c, nights, max_price_per_night)     # an empty list: nothing under the cap


@mcp.tool()
def travel_info() -> dict:
    """This month's live-search usage and SerpAPI's remaining quota. Never returns the key."""
    info = {"backend": "serpapi", "key_set": bool(serp().api_key), **serp().usage()}
    try:
        acct = serp().account() if serp().api_key else {}
        info["searches_left_at_serpapi"] = acct.get("total_searches_left", acct.get("plan_searches_left"))
    except SerpApiError as e:
        info["error"] = str(e)
    return info


if __name__ == "__main__":
    print(f"travel MCP server (SerpAPI) on http://127.0.0.1:{PORT}/mcp")
    mcp.run(transport="streamable-http")
