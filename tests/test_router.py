"""Router rules: the golden set of messages and where each must go."""
import pytest

from veltrix_copilot.orchestrator.router import route

ROUTES = [
    ('What is our leave policy for 2024?', ['rag']),
    ('Summarize project Kestrel.', ['rag']),
    ('Plan a 2-day trip to London next month with a budget of $1500.', ['travel']),
    ('Create a draft email to my manager with the travel plan.', ['email']),
    ('Save this plan to my calendar.', ['calendar']),
    ('Plan a 1-day trip to Paris under $900 and email it to my manager.', ['travel', 'email']),
    ("What's our hotel cap in the travel policy for London?", ['rag']),
    ('What is the email policy?', ['rag']),
    ('Is travel to London allowed without approval?', ['rag']),
    ('Book a hotel in London for 2 nights', ['travel']),
    ('I need to travel to Dubai next week', ['travel']),
    ('Email my manager the plan for the trip', ['email']),
    ('Book a meeting with Sara tomorrow at 3pm', ['calendar']),
    ('How many sick days do I get?', ['rag']),
    ('Plan a trip to London within our travel policy', ['travel']),
    ('Write an email to my manager saying I will be late tomorrow', ['email']),
    ('Add the trip to my calendar and email my manager', ['email', 'calendar']),
    ('Can I work from abroad?', ['rag']),
    ('Send it to my manager', ['email']),
    ('Forward the plan to my boss', ['email']),
    ('Add it to my schedule', ['calendar']),
    ('Add the event to calendar', ['calendar']),
    ('Save to calendar', ['calendar']),
    ('Write to my manager about the trip', ['email']),
    ('How many trips have been planned in November?', ['trips']),
    ('Show me other flights for trip #3', ['trips']),
    ('Make it 3 nights', ['trips']),
    ('Pick a cheaper hotel', ['trips']),
    ('Cancel trip #2', ['trips']),
    ('How many trips can I take per year?', ['rag']),
]


@pytest.mark.parametrize("question,expected", ROUTES)
def test_golden_routes(question, expected):
    assert route(question) == expected


@pytest.mark.parametrize("question", [
    "What's our hotel cap in the travel policy for London?",   # mentions 'hotel' + 'travel'
    "What is the email policy?",                               # mentions 'email'
    "Is travel to London allowed without approval?",           # 'travel to London'
    "How much is the per diem in Paris?",
])
def test_policy_questions_are_not_hijacked_by_tool_keywords(question):
    assert route(question) == ["rag"]


def test_multi_agent_order_is_fixed():
    assert route("Email my manager and save it to my calendar after you plan a trip to Tokyo") == ["travel", "email", "calendar"]


def test_reference_to_existing_plan_does_not_replan():
    assert route("Create a draft email to my manager with the travel plan.") == ["email"]


@pytest.mark.parametrize("message", ["save this trip", "Save the plan", "save it", "keep this trip."])
def test_save_this_trip_goes_to_the_travel_agent(message):
    assert route(message) == ["travel"]


def test_saving_to_the_calendar_is_still_the_calendar():
    assert route("Save this plan to my calendar.") == ["calendar"]
