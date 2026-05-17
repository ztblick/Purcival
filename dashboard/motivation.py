"""Short dashboard title phrases."""

from datetime import date


MOTIVATIONAL_TITLES = [
    "One step at a time",
    "Keep at it, Zach",
    "Onward!",
    "Every day a little closer to your dream",
    "Make the next move",
    "Small wins compound",
    "Keep the signal bright",
]


def title_for_date(day: date | None = None) -> str:
    """Pick one stable title for a calendar day."""
    current_day = day or date.today()
    return MOTIVATIONAL_TITLES[current_day.toordinal() % len(MOTIVATIONAL_TITLES)]
