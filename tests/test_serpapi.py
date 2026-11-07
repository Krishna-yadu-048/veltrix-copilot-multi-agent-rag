"""SerpAPI travel backend: cost guards, caching, key safety and parsing. No real network calls."""
import json
import logging

import httpx
import pytest

from mcp_servers import travel_server
from mcp_servers.serpapi_client import (SerpApiBudgetError, SerpApiClient, SerpApiError, parse_flights,
                                        parse_hotels)

KEY = "sk_test_SECRET_1234567890"
LONDON = {"name": "London", "code": "LHR", "tz": "Europe/London"}
KOCHI = {"name": "Kochi", "code": "COK", "tz": "Asia/Kolkata"}

FLIGHTS = {
    "best_flights": [{
        "flights": [
            {"departure_airport": {"id": "COK", "time": "2026-11-02 04:10"}, "arrival_airport": {"id": "DXB", "time": "2026-11-02 06:40"},
             "airline": "Emirates", "flight_number": "EK 533", "duration": 240},
            {"departure_airport": {"id": "DXB", "time": "2026-11-02 08:15"}, "arrival_airport": {"id": "LHR", "time": "2026-11-02 12:35"},
             "airline": "Emirates", "flight_number": "EK 1", "duration": 500}],
        "total_duration": 865, "price": 612}],
    "other_flights": [
        {"flights": [{"departure_airport": {"id": "COK", "time": "2026-11-02 02:35"}, "arrival_airport": {"id": "LHR", "time": "2026-11-02 08:05"},
                      "airline": "Air India", "flight_number": "AI 149", "duration": 600}], "total_duration": 600, "price": 540},
        {"flights": [{"departure_airport": {"id": "COK", "time": "2026-11-02 10:00"}, "arrival_airport": {"id": "LHR", "time": "2026-11-02 20:00"},
                      "airline": "X", "flight_number": "X 1"}], "total_duration": 900}],  # no price -> skipped
}
HOTELS = {"properties": [
    {"type": "hotel", "name": "Bloomsbury Inn", "rate_per_night": {"extracted_lowest": 165}, "overall_rating": 4.4,
     "reviews": 2100, "extracted_hotel_class": 3,
     "link": "https://example.com/b", "nearby_places": [{"name": "British Museum"}]},
    {"type": "hotel", "name": "Ritz", "rate_per_night": {"extracted_lowest": 900}, "overall_rating": 4.9, "reviews": 5000},
    {"type": "vacation rental", "name": "Flat", "rate_per_night": {"extracted_lowest": 90}, "overall_rating": 4.8,
     "reviews": 300},
    # What the screenshot showed: a café with rooms, near the airport, a perfect score from a few reviews.
    {"type": "hotel", "name": "Abbey Point Cafe", "rate_per_night": {"extracted_lowest": 88}, "overall_rating": 5.0,
     "reviews": 400, "neighborhood": "near Heathrow Airport"},
    {"type": "hotel", "name": "Tiny Guesthouse Hotel", "rate_per_night": {"extracted_lowest": 70}, "overall_rating": 5.0,
     "reviews": 4},
    {"name": "No Type Lodge", "rate_per_night": {"extracted_lowest": 95}, "overall_rating": 4.6, "reviews": 800},
    {"type": "hotel", "name": "Holborn Hotel", "rate_per_night": {"extracted_lowest": 150}, "overall_rating": 4.1,
     "reviews": 900, "extracted_hotel_class": 4},
    {"type": "hotel", "name": "The Kings Arms Pub & Rooms", "rate_per_night": {"extracted_lowest": 120},
     "overall_rating": 4.5, "reviews": 600},
]}


class FakeSerp:
    """httpx transport standing in for serpapi.com. Records every request."""

    def __init__(self, searches_left=200, search_status=200, search_body=None):
        self.requests, self.searches_left = [], searches_left
        self.search_status, self.search_body = search_status, search_body or FLIGHTS

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/account.json":
            return httpx.Response(200, json={"total_searches_left": self.searches_left})
        return httpx.Response(self.search_status, json=self.search_body)

    @property
    def searches(self):
        return [r for r in self.requests if r.url.path == "/search.json"]


def client(tmp_path, fake, **kw) -> SerpApiClient:
    return SerpApiClient(KEY, tmp_path / "cache", tmp_path / "usage.json",
                         http=httpx.Client(transport=httpx.MockTransport(fake)), **kw)


PARAMS = {"engine": "google_flights", "departure_id": "COK", "arrival_id": "LHR", "outbound_date": "2026-11-02", "type": "2"}


def test_identical_searches_are_served_from_cache(tmp_path):
    fake = FakeSerp()
    c = client(tmp_path, fake)
    assert c.search(PARAMS)["_served_from"] == "serpapi"
    assert c.search(PARAMS)["_served_from"] == "local-cache"
    assert len(fake.searches) == 1 and c.usage()["live_searches"] == 1


def test_monthly_safety_limit_stops_live_searches(tmp_path):
    fake = FakeSerp()
    c = client(tmp_path, fake, monthly_limit=2)
    c.search({**PARAMS, "outbound_date": "2026-11-02"})
    c.search({**PARAMS, "outbound_date": "2026-11-03"})
    with pytest.raises(SerpApiBudgetError, match="monthly safety limit"):
        c.search({**PARAMS, "outbound_date": "2026-11-04"})
    assert len(fake.searches) == 2


def test_stops_when_serpapi_quota_is_nearly_used(tmp_path):
    fake = FakeSerp(searches_left=15)
    with pytest.raises(SerpApiBudgetError, match="15 SerpAPI searches left"):
        client(tmp_path, fake, min_searches_left=20).search(PARAMS)
    assert fake.searches == []  # the quota check is free and happens BEFORE any search


def test_no_key_means_no_request(tmp_path):
    fake = FakeSerp()
    c = SerpApiClient("", tmp_path / "c", tmp_path / "u.json", http=httpx.Client(transport=httpx.MockTransport(fake)))
    with pytest.raises(SerpApiBudgetError, match="no SERPAPI_API_KEY"):
        c.search(PARAMS)
    assert fake.requests == []


def test_key_never_appears_in_errors_logs_or_cache(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(request):
        raise httpx.ConnectError(f"failed to connect to {request.url}")  # real httpx errors include the URL
    c = SerpApiClient(KEY, tmp_path / "cache", tmp_path / "u.json", http=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(SerpApiError) as exc:
        c.search(PARAMS)
    assert KEY not in str(exc.value) and exc.value.__cause__ is None and exc.value.__suppress_context__
    assert KEY not in caplog.text

    bad = client(tmp_path, FakeSerp(search_status=401, search_body={"error": f"Invalid API key {KEY}"}))
    with pytest.raises(SerpApiError) as exc2:
        bad.search({**PARAMS, "outbound_date": "2026-12-01"})
    assert KEY not in str(exc2.value)

    ok = client(tmp_path / "x", FakeSerp())
    ok.search(PARAMS)
    for f in (tmp_path / "x").rglob("*.json"):
        assert KEY not in f.read_text()


def test_only_travel_parameters_are_sent(tmp_path):
    fake = FakeSerp()
    client(tmp_path, fake).search(PARAMS)
    sent = dict(fake.searches[0].url.params)
    assert set(sent) == set(PARAMS) | {"api_key", "output"}
    assert fake.searches[0].url.scheme == "https" and fake.searches[0].url.host == "serpapi.com"


def test_parse_flights():
    flights = parse_flights(FLIGHTS, KOCHI, LONDON)
    assert [f["price_usd"] for f in flights] == [540, 612]
    nonstop, onestop = flights
    assert nonstop["flight_no"] == "AI149" and nonstop["depart"] == "2026-11-02T02:35" and nonstop["stops"] == 0
    assert nonstop["layovers"] == [] and len(nonstop["legs"]) == 1
    assert onestop["airline"] == "Emirates" and onestop["flight_no"] == "EK533 + EK1" and onestop["arrive"] == "2026-11-02T12:35"
    assert onestop["duration_hours"] == 14.4 and onestop["arrive_tz"] == "Europe/London"
    assert [(leg["from"], leg["to"]) for leg in onestop["legs"]] == [("COK", "DXB"), ("DXB", "LHR")]
    # No 'layovers' block in the response: the wait is worked out from the two local times at DXB.
    assert onestop["layovers"] == [{"airport": "DXB", "name": "", "minutes": 95, "overnight": False}]
    assert all(f["source"] == "serpapi" for f in flights)


def test_parse_flights_reads_layovers_and_mixed_airlines():
    data = {"best_flights": [{
        "flights": [
            {"departure_airport": {"id": "COK", "name": "Cochin", "time": "2026-11-02 23:35"},
             "arrival_airport": {"id": "BOM", "name": "Mumbai", "time": "2026-11-03 01:40"}, "airline": "IndiGo",
             "flight_number": "6E 5199"},
            {"departure_airport": {"id": "BOM", "time": "2026-11-03 09:00"},
             "arrival_airport": {"id": "LHR", "name": "Heathrow", "time": "2026-11-03 14:05"},
             "airline": "British Airways", "flight_number": "BA 138"}],
        "layovers": [{"duration": 440, "name": "Chhatrapati Shivaji Maharaj International Airport", "id": "BOM",
                      "overnight": True}],
        "total_duration": 1020, "price": 443}]}
    (f,) = parse_flights(data, KOCHI, LONDON)
    assert f["airline"] == "IndiGo + British Airways" and f["flight_no"] == "6E5199 + BA138"
    assert f["layovers"][0]["minutes"] == 440 and f["layovers"][0]["overnight"] is True


def test_parse_hotels_keeps_real_hotels_only():
    hotels = parse_hotels(HOTELS, LONDON, nights=1, max_price=180)
    # Out: over the cap (Ritz), not a hotel (Flat), a café (Abbey Point Cafe), too few reviews
    # (Tiny Guesthouse), no type (No Type Lodge). A pub that says "Rooms" counts as lodging.
    assert [h["name"] for h in hotels] == ["Bloomsbury Inn", "The Kings Arms Pub & Rooms", "Holborn Hotel"]
    assert hotels[0]["area"] == "near British Museum" and hotels[0]["link"] == "https://example.com/b"


def test_hotel_ranking_weighs_reviews():
    from mcp_servers.serpapi_client import hotel_score
    assert hotel_score(5.0, 4, None) < hotel_score(4.4, 2000, None)
    assert hotel_score(4.3, 900, 4) > hotel_score(4.3, 900, 3)


def test_hotel_search_asks_google_for_real_hotels(serp_backend):
    fake = FakeSerp(search_body=HOTELS)
    serp_backend(fake)
    hotels = travel_server.search_hotels("London", "2026-11-03", 1, 180)
    sent = [r for r in fake.requests if "google_hotels" in str(r.url)][0]
    assert sent.url.params["hotel_class"] == "3,4,5" and sent.url.params["rating"] == "7"
    assert hotels[0]["name"] == "Bloomsbury Inn" and hotels[0]["source"] == "serpapi"


# ---------------------------------------------------------------- travel server: live only, clear errors
@pytest.fixture
def serp_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))

    def install(fake=None, key=KEY, **kw):
        c = SerpApiClient(key, tmp_path / "cache", tmp_path / "usage.json",
                          http=httpx.Client(transport=httpx.MockTransport(fake or FakeSerp())), **kw)
        monkeypatch.setattr(travel_server, "_serp", c)
        return c
    return install


def test_server_uses_live_results(serp_backend):
    serp_backend(FakeSerp(search_body=FLIGHTS))
    flights = travel_server.search_flights("Kochi", "London", "2026-11-02")
    assert flights[0]["source"] == "serpapi" and flights[0]["price_usd"] == 540


def test_server_reports_a_clear_error_when_a_guard_trips(serp_backend):
    serp_backend(FakeSerp(searches_left=5))
    with pytest.raises(ValueError, match="Live travel search failed: .*searches left"):
        travel_server.search_flights("Kochi", "London", "2026-11-02")


def test_server_reports_a_missing_key(serp_backend):
    serp_backend(key="")
    with pytest.raises(ValueError, match="SERPAPI_API_KEY"):
        travel_server.search_hotels("London", "2026-11-02", 1, 180)


def test_no_flights_is_an_error_not_made_up_data(serp_backend):
    serp_backend(FakeSerp(search_body={"best_flights": [], "other_flights": []}))
    with pytest.raises(ValueError, match="No live flights found"):
        travel_server.search_flights("Kochi", "London", "2026-11-02")


def test_travel_info_never_returns_the_key(serp_backend):
    serp_backend()
    info = travel_server.travel_info()
    assert info["backend"] == "serpapi" and info["key_set"] is True
    assert KEY not in json.dumps(info)
