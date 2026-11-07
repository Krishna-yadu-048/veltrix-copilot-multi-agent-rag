"""Pydantic models: what the LLM must return (structured output) and what the API speaks."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TripRequest(BaseModel):
    destination: Optional[str] = Field(None, description="City to travel to, e.g. 'London'")
    origin: Optional[str] = Field(None, description="City travelling from, if the user said it")
    days: Optional[int] = Field(None, description="Length of the trip in days, if stated")
    budget_usd: Optional[float] = Field(None, description="Total budget in US dollars, if stated")
    start_date: Optional[str] = Field(None, description="Start date as YYYY-MM-DD if the user gave an exact date")


class EmailDraft(BaseModel):
    subject: str = Field(..., description="Short email subject line")
    opening: str = Field(..., description="The main message text, 1-4 sentences")
    closing: str = Field(..., description="1 sentence that closes the email, e.g. asking for approval")


class EventRequest(BaseModel):
    title: str
    date: Optional[str] = Field(None, description="YYYY-MM-DD")
    start_time: Optional[str] = Field(None, description="HH:MM 24-hour")
    duration_minutes: int = 60


class RagAnswer(BaseModel):
    found: bool = Field(..., description="true if the documents contain the answer, false otherwise")
    answer: str = Field("", description="The answer with inline citations like [doc.md#0]; empty if not found")


class Itinerary(BaseModel):
    days: list[str] = Field(..., description="One short line per day, e.g. 'Day 1 (Mon 02 Nov): Arrive and check in.'")


class FollowUpDecision(BaseModel):
    about_saved_trip: bool = Field(..., description="true if the message is about the saved trip")
    confident: bool = Field(..., description="false if the message could reasonably mean something else")


class Source(BaseModel):
    doc_name: str
    chunk_index: int
    snippet: str
    page: Optional[int] = None        # PDF page number (1-based)
    section: Optional[str] = None     # heading the passage sits under (Markdown / Word)
    quote: str = ""                   # the sentence the answer relied on (found in code)
    passage: str = ""                 # the full passage, so the user can check it


class ChatRequest(BaseModel):
    session_id: str = Field(..., pattern=r"^[0-9a-f]{32}$", description="From POST /session")
    message: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source] = []
    actions: list[str] = []
    plan: list[str] = []
    timings: dict[str, float] = {}
    ask: Optional[str] = None      # "yes_no" when the answer ends with a yes/no question (the app shows buttons)


class TripPicks(BaseModel):
    outbound: Optional[str] = Field(None, max_length=120, description="flight_key of the chosen outbound option")
    return_: Optional[str] = Field(None, alias="return", max_length=120)
    hotel: Optional[str] = Field(None, max_length=200)

    model_config = {"populate_by_name": True}


class TripChange(BaseModel):
    """What the Trips page sends: changed inputs and chosen options. Prices are always computed on the server."""
    origin: Optional[str] = Field(None, max_length=60)
    destination: Optional[str] = Field(None, max_length=60)
    start: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    nights: Optional[int] = Field(None, ge=1, le=14)
    budget_amount: Optional[float] = Field(None, gt=0, le=10_000_000)
    budget_currency: Optional[str] = Field(None, max_length=3)
    no_budget: bool = False
    picks: TripPicks = TripPicks()

    def as_change(self) -> dict:
        d = self.model_dump(exclude={"picks"})
        d["picks"] = {"outbound": self.picks.outbound, "return": self.picks.return_, "hotel": self.picks.hotel}
        return d


class TripApply(TripChange):
    base_updated_at: str = Field(..., max_length=64, description="updated_at from the preview (stale-edit check)")
    email: bool = True
    calendar: bool = True


class ResetRequest(BaseModel):
    confirm: str = Field(..., max_length=20, description='Must be exactly "CONFIRM"')
