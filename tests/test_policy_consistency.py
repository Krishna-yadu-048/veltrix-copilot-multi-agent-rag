"""The planner's numbers (travel_rules.py) must match the Veltrix travel policy document, POL-FIN-001 v2.0."""
from pathlib import Path

from veltrix_copilot.core import currency
from veltrix_copilot.knowledge import doc_versions
from veltrix_copilot.travel import travel_rules as r
from veltrix_copilot.travel.policy_checks import policy_document

DOCS = Path(__file__).resolve().parents[1] / "data" / "docs"
POLICY = (DOCS / "policies" / "pol-fin-001-travel-policy.md").read_text(encoding="utf-8")


def _money(amount, code):
    return f"**{currency.fmt(amount, code)}**"


def test_policy_version():
    assert "| Version | 2.0 |" in POLICY and "| Status | Active |" in POLICY and r.POLICY_ID == "POL-FIN-001 v2.0"


def test_hotel_caps_and_per_diems_match_the_policy():
    caps = POLICY.split("3.3.")[1].split("3.4.")[0]
    per_diems = POLICY.split("3.4.")[1].split("3.5.")[0]
    for (amount, code) in r.HOTEL_CAP.values():
        assert _money(amount, code) in caps
    for (amount, code) in r.PER_DIEM.values():
        assert _money(amount, code) in per_diems


def test_booking_cabin_and_approval_rules_match_the_policy():
    assert f"at least **{r.DOMESTIC_NOTICE_DAYS} days** in advance for domestic" in POLICY
    assert f"**{r.INTERNATIONAL_NOTICE_DAYS} days** for international" in POLICY
    assert f"longer than **{r.PREMIUM_ECONOMY_MIN_HOURS} hours** may be booked in premium economy" in POLICY
    assert f"above **${r.DEPT_HEAD_APPROVAL_OVER_USD:,}** per trip require department head approval" in POLICY


def test_caps_converted_to_usd_never_exceed_the_policy():
    for city in ["Kochi", "Bangalore", "Pune", "Berlin", "Singapore", "Austin"]:
        lim = r.limits(city)
        amount, code = r.HOTEL_CAP[lim["region"]]
        assert lim["covered"] and currency.convert(lim["hotel_cap_usd"], code) <= amount


def test_regions():
    assert r.region("Bangalore") == "india_tier1" and r.region("Kochi") == "india_other"
    assert r.region("Berlin") == "eu" and r.region("Singapore") == "sg" and r.region("Austin") == "us"
    assert r.region("London") is None and r.limits("London")["covered"] is False
    assert r.is_domestic("Kochi", "Pune") and not r.is_domestic("Kochi", "Berlin")


def test_the_current_policy_is_used_and_the_old_one_is_superseded():
    assert policy_document()[0] == "policies/pol-fin-001-travel-policy.md"
    old = "policies/archive/pol-fin-001-travel-policy-v1-3-superseded.md"
    assert doc_versions.superseded([old, "policies/pol-fin-001-travel-policy.md"]) == {old: "policies/pol-fin-001-travel-policy.md"}
    assert doc_versions.skipped_for("What is the hotel cap in Singapore?", [old, "policies/pol-fin-001-travel-policy.md"])
    assert not doc_versions.skipped_for("What did the previous travel policy allow?", [old, "policies/pol-fin-001-travel-policy.md"])
