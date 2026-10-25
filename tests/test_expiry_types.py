from datetime import date
from src.options_math import classify_expiry


def test_classify_expiry():
    # 2026-10-19 is Monday
    assert classify_expiry(date(2026, 10, 19)) == "daily"
    # 2026-10-20 is Tuesday
    assert classify_expiry(date(2026, 10, 20)) == "daily"
    # 2026-10-21 is Wednesday
    assert classify_expiry(date(2026, 10, 21)) == "daily"
    # 2026-10-22 is Thursday
    assert classify_expiry(date(2026, 10, 22)) == "daily"
    # 2026-10-24 is Saturday
    assert classify_expiry(date(2026, 10, 24)) == "daily"
    # 2026-10-25 is Sunday
    assert classify_expiry(date(2026, 10, 25)) == "daily"

    # Fridays in October 2026:
    # 2026-10-02 (Friday, not last) -> weekly
    assert classify_expiry(date(2026, 10, 2)) == "weekly"
    # 2026-10-09 (Friday, not last) -> weekly
    assert classify_expiry(date(2026, 10, 9)) == "weekly"
    # 2026-10-16 (Friday, not last) -> weekly
    assert classify_expiry(date(2026, 10, 16)) == "weekly"
    # 2026-10-23 (Friday, not last) -> weekly
    assert classify_expiry(date(2026, 10, 23)) == "weekly"
    # 2026-10-30 (Friday, last Friday of Oct) -> monthly
    assert classify_expiry(date(2026, 10, 30)) == "monthly"

    # Quarterly expiries: last Friday of March, June, September, December
    # 2026-03-27 is last Friday of March -> quarterly
    assert classify_expiry(date(2026, 3, 27)) == "quarterly"
    # 2026-06-26 is last Friday of June -> quarterly
    assert classify_expiry(date(2026, 6, 26)) == "quarterly"
    # 2026-09-25 is last Friday of September -> quarterly
    assert classify_expiry(date(2026, 9, 25)) == "quarterly"
    # 2026-12-25 is last Friday of December -> quarterly
    assert classify_expiry(date(2026, 12, 25)) == "quarterly"

    # Monthly non-quarterly:
    # 2026-01-30 is last Friday of January -> monthly
    assert classify_expiry(date(2026, 1, 30)) == "monthly"
    # 2026-02-27 is last Friday of February -> monthly
    assert classify_expiry(date(2026, 2, 27)) == "monthly"
    # 2026-04-24 is last Friday of April -> monthly
    assert classify_expiry(date(2026, 4, 24)) == "monthly"
