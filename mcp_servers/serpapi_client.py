"""SerpAPI client with cost and leak protection, used by the travel MCP server."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

# httpx logs full request URLs at INFO level, and the URL contains api_key: never let that through.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SEARCH_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"


class SerpApiError(RuntimeError):
    """Live search failed (bad key, network, SerpAPI error). Message is safe to show."""


class SerpApiBudgetError(SerpApiError):
    """A cost guard stopped the search."""


class SerpApiClient:
    def __init__(self, api_key: str, cache_dir: Path, usage_file: Path, monthly_limit: int = 100,
                 min_searches_left: int = 20, cache_hours: float = 24, http: httpx.Client | None = None):
        self.api_key = (api_key or "").strip()
        self.cache_dir = cache_dir
        self.usage_file = usage_file
        self.monthly_limit = monthly_limit
        self.min_left = min_searches_left
        self.cache_seconds = cache_hours * 3600
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)
        self._account: tuple[float, dict] | None = None

    def search(self, params: dict) -> dict:
        """Run (or reuse) one SerpAPI search. `params` must NOT contain api_key."""
        assert "api_key" not in params
        key = self._cache_key(params)
        cached = self._cache_get(key)
        if cached is not None:
            return {**cached, "_served_from": "local-cache"}
        if not self.api_key:
            raise SerpApiBudgetError("no SERPAPI_API_KEY set")
        self._check_budget()
        data = self._get(SEARCH_URL, {**params, "output": "json"})
        self._count_search()
        self._cache_put(key, data)
        return {**data, "_served_from": "serpapi"}

    def usage(self) -> dict:
        u = self._read_usage()
        return {"month": u["month"], "live_searches": u["calls"], "monthly_limit": self.monthly_limit}

    def account(self) -> dict:
        """Remaining quota from SerpAPI (free call, not counted). Cached for 10 minutes."""
        if self._account and time.time() - self._account[0] < 600:
            return self._account[1]
        data = self._get(ACCOUNT_URL, {})
        self._account = (time.time(), data)
        return data

    def _check_budget(self) -> None:
        u = self._read_usage()
        if u["calls"] >= self.monthly_limit:
            raise SerpApiBudgetError(f"monthly safety limit of {self.monthly_limit} live searches reached")
        acct = self.account()
        left = acct.get("total_searches_left", acct.get("plan_searches_left"))
        if left is not None and left <= self.min_left:
            raise SerpApiBudgetError(f"only {left} SerpAPI searches left this month (keeping {self.min_left} in reserve)")

    def _read_usage(self) -> dict:
        month = datetime.now().strftime("%Y-%m")
        try:
            u = json.loads(self.usage_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            u = {}
        return u if u.get("month") == month else {"month": month, "calls": 0}

    def _count_search(self) -> None:
        u = self._read_usage()
        u["calls"] += 1
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps(u), encoding="utf-8")

    def _get(self, url: str, params: dict) -> dict:
        try:
            r = self.http.get(url, params={**params, "api_key": self.api_key})
        except httpx.HTTPError as e:
            # `from None`: the original exception's message includes the URL, which contains the key.
            raise SerpApiError(f"could not reach SerpAPI ({type(e).__name__})") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 401:
            raise SerpApiError("SerpAPI rejected the API key (check SERPAPI_API_KEY)")
        if r.status_code == 429:
            raise SerpApiBudgetError("SerpAPI says the plan's search limit is reached")
        if r.status_code >= 400 or data.get("error"):
            msg = str(data.get("error") or f"HTTP {r.status_code}")
            raise SerpApiError(f"SerpAPI error: {msg.replace(self.api_key, '***')[:200]}")
        return data

    def _cache_key(self, params: dict) -> str:
        return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:32]

    def _cache_get(self, key: str) -> dict | None:
        path = self.cache_dir / f"{key}.json"
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return entry["data"] if time.time() - entry.get("saved_at", 0) < self.cache_seconds else None

    def _cache_put(self, key: str, data: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{key}.json").write_text(json.dumps({"saved_at": time.time(), "data": data}), encoding="utf-8")


def _iso(t: str) -> str:
    """SerpAPI times look like '2026-11-02 08:35' -> '2026-11-02T08:35'."""
    return (t or "").replace(" ", "T")[:16]


def _minutes_between(a: str, b: str) -> int | None:
    try:
        return int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() // 60)
    except ValueError:
        return None


def parse_flights(data: dict, origin: dict, dest: dict) -> list[dict]:
    """Every itinerary with ALL its legs and layovers (connections included), sorted by price.
    The travel agent then picks one by price, stops and total time (see veltrix_copilot/travel/travel_rules.py)."""
    out = []
    for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        raw, price = it.get("flights") or [], it.get("price")
        if not raw or not isinstance(price, (int, float)):
            continue
        legs = []
        for leg in raw:
            dep, arr = leg.get("departure_airport") or {}, leg.get("arrival_airport") or {}
            legs.append({"flight_no": (leg.get("flight_number") or "").replace(" ", "") or "—",
                         "airline": leg.get("airline") or "Unknown",
                         "from": dep.get("id", ""), "from_name": dep.get("name", ""),
                         "to": arr.get("id", ""), "to_name": arr.get("name", ""),
                         "depart": _iso(dep.get("time", "")), "arrive": _iso(arr.get("time", "")),
                         "minutes": leg.get("duration")})
        if any(len(leg["depart"]) != 16 or len(leg["arrive"]) != 16 for leg in legs):
            continue
        given = it.get("layovers") or []
        layovers = []
        for k in range(len(legs) - 1):
            info = given[k] if k < len(given) else {}
            minutes = info.get("duration")
            if not isinstance(minutes, (int, float)):   # same airport, both local times: the gap is the wait
                minutes = _minutes_between(legs[k]["arrive"], legs[k + 1]["depart"])
            layovers.append({"airport": info.get("id") or legs[k]["to"], "name": info.get("name") or legs[k]["to_name"],
                             "minutes": int(minutes) if minutes is not None else None,
                             "overnight": bool(info.get("overnight"))})
        airlines = list(dict.fromkeys(leg["airline"] for leg in legs))
        out.append({
            "flight_no": " + ".join(leg["flight_no"] for leg in legs),
            "airline": " + ".join(airlines),
            "origin": origin["name"], "destination": dest["name"],
            "depart": legs[0]["depart"], "arrive": legs[-1]["arrive"],
            "depart_tz": origin.get("tz", ""), "arrive_tz": dest.get("tz", ""),
            "duration_hours": round((it.get("total_duration") or 0) / 60, 1),
            "stops": len(legs) - 1, "legs": legs, "layovers": layovers,
            "cabin": "economy",
            "price_usd": int(round(price)),
            "source": "serpapi",
        })
    return sorted(out, key=lambda f: f["price_usd"])[:15]


# Listings Google Hotels shows that are really somewhere to eat or drink (rooms above a pub or café).
_NOT_LODGING_RE = re.compile(r"\b(caf[eé]|coffee|restaurant|bistro|brasserie|bar|pub|grill|kitchen|eatery|diner|"
                             r"bakery|tavern|takeaway)\b", re.I)
_LODGING_RE = re.compile(r"\b(hotel|inn|suites?|lodge|resort|residenc[ey]|aparthotel|hostel|motel|rooms)\b", re.I)
MIN_REVIEWS = 20


def looks_like_a_hotel(name: str) -> bool:
    return not _NOT_LODGING_RE.search(name) or bool(_LODGING_RE.search(name))


def hotel_score(rating: float, reviews: int, hotel_class: int | None) -> float:
    """Rating adjusted for how many reviews it rests on (5.0 from 4 reviews < 4.4 from 2,000), plus a
    small bonus per star."""
    prior, weight = 4.0, 50
    return (rating * reviews + prior * weight) / (reviews + weight) + 0.05 * (hotel_class or 0)


def parse_hotels(data: dict, city: dict, nights: int, max_price: float) -> list[dict]:
    """Real hotels only: type 'hotel', a hotel-like name, enough reviews, under the nightly cap."""
    out = []
    for p in data.get("properties") or []:
        name = p.get("name") or ""
        if p.get("type") != "hotel" or not name or not looks_like_a_hotel(name):
            continue
        rate = (p.get("rate_per_night") or {}).get("extracted_lowest")
        if not isinstance(rate, (int, float)) or rate > max_price:
            continue
        reviews = int(p.get("reviews") or 0)
        if reviews < MIN_REVIEWS:
            continue
        rating, stars = float(p.get("overall_rating") or 0), p.get("extracted_hotel_class")
        nearby = (p.get("nearby_places") or [{}])[0].get("name")
        out.append({
            "name": name,
            "area": p.get("neighborhood") or (f"near {nearby}" if nearby else city["name"]),
            "price_per_night": int(round(rate)),
            "rating": rating, "reviews": reviews,
            "hotel_class": stars,
            "score": round(hotel_score(rating, reviews, stars), 3),
            "link": p.get("link"),
            "city": city["name"], "nights": nights, "total_usd": int(round(rate)) * nights,
            "source": "serpapi",
        })
    return sorted(out, key=lambda h: (-h["score"], h["price_per_night"]))[:10]
