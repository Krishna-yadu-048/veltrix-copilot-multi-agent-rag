from datetime import date

from veltrix_copilot.core.dates import first_monday, has_date_hint, next_month_start, parse_time, resolve_start_date

TODAY = date(2026, 10, 29)  # a Thursday


def test_next_month_is_first_monday_of_next_month():
    assert resolve_start_date("trip next month", TODAY) == date(2026, 11, 2)


def test_december_rolls_over_to_january():
    assert next_month_start(date(2026, 12, 10)) == first_monday(2027, 1)


def test_explicit_iso_date_wins():
    assert resolve_start_date("fly on 2026-12-14 next month", TODAY) == date(2026, 12, 14)


def test_past_iso_date_is_ignored():
    assert resolve_start_date("on 2020-01-01", TODAY) != date(2020, 1, 1)


def test_month_name_with_day():
    assert resolve_start_date("in December, around December 3rd", TODAY) == date(2026, 12, 3)


def test_past_month_goes_to_next_year():
    assert resolve_start_date("in March", TODAY).year == 2027


def test_weekday_and_tomorrow():
    assert resolve_start_date("on Monday", TODAY) == date(2026, 11, 2)
    assert resolve_start_date("tomorrow", TODAY) == date(2026, 10, 30)


def test_llm_date_used_only_as_fallback():
    assert resolve_start_date("a trip", TODAY, "2026-11-20") == date(2026, 11, 20)
    assert resolve_start_date("a trip next week", TODAY, "2026-11-20") == date(2026, 11, 2)


def test_default_is_a_future_monday():
    d = resolve_start_date("a trip", TODAY)
    assert d > TODAY and d.weekday() == 0


def test_parse_time():
    assert parse_time("3pm") == "15:00"
    assert parse_time("9:30 am") == "09:30"
    assert parse_time("12am") == "00:00"
    assert parse_time("nonsense") == "09:00"


def test_date_hints():
    assert has_date_hint("meeting tomorrow at 3pm")
    assert has_date_hint("on 2026-12-01")
    assert not has_date_hint("Save this plan to my calendar.")
    assert not has_date_hint("you may add it")  # 'may' the verb is not a month
