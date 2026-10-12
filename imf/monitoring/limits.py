"""Limit classification and persistence-based event generation.

Levels: 0 = NOMINAL, 1 = WARNING (soft limit), 2 = ALARM (hard limit).
A level change is only confirmed after `persistence` consecutive samples at that level,
which suppresses events from single outliers and gives hysteresis on the way back to nominal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Event, Limit, Parameter, ParameterState, LEVEL_NAMES

NOMINAL, WARNING, ALARM = 0, 1, 2


def classify(value: float, limit: Limit | None) -> tuple[int, str]:
    """Return (level, human-readable violated limit)."""
    if limit is None:
        return NOMINAL, ""
    if limit.alarm_hi is not None and value > limit.alarm_hi:
        return ALARM, f"> {limit.alarm_hi:g} (alarm high)"
    if limit.alarm_lo is not None and value < limit.alarm_lo:
        return ALARM, f"< {limit.alarm_lo:g} (alarm low)"
    if limit.warn_hi is not None and value > limit.warn_hi:
        return WARNING, f"> {limit.warn_hi:g} (warning high)"
    if limit.warn_lo is not None and value < limit.warn_lo:
        return WARNING, f"< {limit.warn_lo:g} (warning low)"
    return NOMINAL, ""


@dataclass
class CheckResult:
    level: int
    new_events: list[Event]
    cleared: list[Event]


def check_sample(s: Session, param: Parameter, value: float, time: datetime,
                 product_id: int | None) -> CheckResult:
    limit = param.current_limit
    level, limit_text = classify(value, limit)
    persistence = max(1, limit.persistence if limit else 1)

    st = param.state
    if st is None:
        st = ParameterState(parameter_id=param.id, level=0, candidate_level=0, candidate_count=0)
        s.add(st)
        param.state = st

    if level == st.candidate_level:
        st.candidate_count += 1
    else:
        st.candidate_level, st.candidate_count = level, 1
    st.last_value, st.last_time = value, time

    new_events: list[Event] = []
    cleared: list[Event] = []
    open_events = list(s.scalars(select(Event).where(
        Event.parameter_id == param.id, Event.cleared_at.is_(None))))

    # Update running statistics of events that are still active
    for ev in open_events:
        if level >= _rank(ev.severity):
            ev.n_violations += 1
            ev.last_time, ev.last_value = time, value
            if ev.worst_value is None or abs(value - ev.value) > abs(ev.worst_value - ev.value):
                ev.worst_value = value

    if st.candidate_count >= persistence and level != st.level:
        previous, st.level = st.level, level
        if level > previous:  # escalation -> new event
            ev = Event(parameter_id=param.id, product_id=product_id, severity=LEVEL_NAMES[level],
                       time=time, value=value, limit_text=limit_text, last_time=time, last_value=value,
                       worst_value=value, n_violations=persistence, status="OPEN")
            s.add(ev)
            new_events.append(ev)
        else:  # de-escalation -> clear events above the new level
            for ev in open_events:
                if _rank(ev.severity) > level:
                    ev.cleared_at = time
                    if ev.status == "ACKNOWLEDGED":
                        ev.status = "CLOSED"
                    cleared.append(ev)
    return CheckResult(level, new_events, cleared)


def _rank(severity: str) -> int:
    return ALARM if severity == "ALARM" else WARNING
