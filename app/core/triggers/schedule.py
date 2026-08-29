"""
Schedule triggers
=================
Cron expressions, and the two decisions that make a scheduler behave sanely.

**Timezones.** "Every weekday at 9am" means 9am where the user lives. Storing the cron in UTC
would work until March, when the clocks move and the agent starts running at 8am. So the
trigger carries an IANA timezone, the next run is computed in that zone, and only the result
is converted to UTC for storage.

**Missed runs are dropped, not replayed.** If the worker is down for six hours, an hourly
agent has six occurrences in the past. Firing all six on restart would send the user six
inbox digests in one minute. The next run is therefore always computed forward from now, so
a gap in coverage costs you the runs you missed and nothing more.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter

DEFAULT_TIMEZONE = "UTC"

# A minute is the finest cron can express, but a minutely agent that takes 90 seconds to run
# would never finish before its next turn. The overlap guard would then skip most runs, which
# looks like a broken scheduler rather than a misconfiguration.
MIN_INTERVAL_SECONDS = 300

# Plain-language presets for the builder. Anything else is entered as raw cron.
PRESETS = {
    "every_15_minutes": "*/15 * * * *",
    "every_30_minutes": "*/30 * * * *",
    "hourly": "0 * * * *",
    "every_weekday_9am": "0 9 * * 1-5",
    "daily_9am": "0 9 * * *",
    "weekly_monday_9am": "0 9 * * 1",
}


class InvalidSchedule(ValueError):
    """The cron expression or timezone cannot be used. Message is safe to show a user."""


def resolve_expression(config: dict) -> str:
    """The cron string for a trigger config, expanding a preset if one was named."""
    preset = config.get("preset")
    if preset:
        if preset not in PRESETS:
            raise InvalidSchedule(f"Unknown schedule preset {preset!r}.")
        return PRESETS[preset]
    expression = str(config.get("cron", "")).strip()
    if not expression:
        raise InvalidSchedule("A schedule needs either a preset or a cron expression.")
    return expression


def resolve_timezone(config: dict) -> ZoneInfo:
    name = str(config.get("timezone") or DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidSchedule(f"Unknown timezone {name!r}.") from exc


def validate(config: dict) -> dict:
    """Check a schedule config and return it normalised, or raise `InvalidSchedule`.

    Runs at the API boundary so a bad cron is a 422 at save time rather than a trigger that
    silently never fires.
    """
    expression = resolve_expression(config)
    tz = resolve_timezone(config)

    try:
        itr = croniter(expression, datetime.now(tz))
    except (CroniterBadCronError, KeyError, ValueError) as exc:
        raise InvalidSchedule(f"{expression!r} is not a valid cron expression.") from exc

    first = itr.get_next(datetime)
    second = itr.get_next(datetime)
    if (second - first).total_seconds() < MIN_INTERVAL_SECONDS:
        raise InvalidSchedule(
            f"Schedules must be at least {MIN_INTERVAL_SECONDS // 60} minutes apart. "
            "An agent run can take a couple of minutes, so anything tighter would overlap "
            "with itself."
        )

    return {"cron": expression, "timezone": str(tz), "preset": config.get("preset")}


def next_run_after(config: dict, after: datetime | None = None) -> datetime:
    """The next UTC instant this schedule should fire, strictly after `after`.

    Defaults to now, which is what drops missed occurrences rather than replaying them.

    Raises `InvalidSchedule` for anything unusable, including a config that was valid when
    saved. Rows do get edited by hand and presets do get renamed, and the scheduler claims
    triggers in batches — letting croniter's own exception escape would take out every other
    agent's schedule along with the broken one.
    """
    tz = resolve_timezone(config)
    expression = resolve_expression(config)
    base = (after or datetime.now(UTC)).astimezone(tz)
    try:
        return croniter(expression, base).get_next(datetime).astimezone(UTC)
    except (CroniterBadCronError, KeyError, ValueError) as exc:
        raise InvalidSchedule(f"{expression!r} is not a valid cron expression.") from exc


def describe(config: dict) -> str:
    """A human-readable summary for the builder and the triggers list."""
    try:
        expression = resolve_expression(config)
    except InvalidSchedule as exc:
        return str(exc)

    tz = str(config.get("timezone") or DEFAULT_TIMEZONE)
    for name, preset_expression in PRESETS.items():
        if preset_expression == expression:
            return f"{name.replace('_', ' ').capitalize()} ({tz})"
    return f"{expression} ({tz})"
