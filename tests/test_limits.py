"""Limit checking: persistence, escalation, clearing."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from imf.db import Database
from imf.models import Event, Limit, Parameter, ParameterState
from imf.monitoring.limits import ALARM, NOMINAL, WARNING, check_sample, classify


def test_classify():
    lim = Limit(warn_lo=-2, warn_hi=2, alarm_lo=-3, alarm_hi=3)
    assert classify(0.0, lim)[0] == NOMINAL
    assert classify(2.5, lim)[0] == WARNING
    assert classify(-2.5, lim)[0] == WARNING
    assert classify(3.5, lim)[0] == ALARM
    assert classify(1e9, None)[0] == NOMINAL


@pytest.fixture
def db_param():
    db = Database("sqlite://")
    db.create_all()
    with db.session() as s:
        p = Parameter(code="X", name="X", group="G", algorithm="a")
        s.add(p)
        s.flush()
        s.add(Limit(parameter_id=p.id, warn_hi=1.0, alarm_hi=2.0, persistence=2))
        s.add(ParameterState(parameter_id=p.id))
    return db


def feed(db, values):
    t0 = datetime(2026, 1, 1)
    with db.session() as s:
        p = s.scalar(select(Parameter))
        for i, v in enumerate(values):
            check_sample(s, p, v, t0 + timedelta(hours=i), None)
            s.flush()
        return [(e.severity, e.cleared_at is not None, e.n_violations) for e in
                s.scalars(select(Event).order_by(Event.id))]


def test_single_outlier_is_suppressed_by_persistence(db_param):
    assert feed(db_param, [0, 1.5, 0, 1.5, 0]) == []


def test_warning_then_alarm_then_clear(db_param):
    events = feed(db_param, [0, 1.5, 1.5, 2.5, 2.5, 2.5, 0, 0])
    assert [e[0] for e in events] == ["WARNING", "ALARM"]
    assert all(cleared for _, cleared, _ in events)


def test_cleared_acknowledged_event_is_closed(db_param):
    feed(db_param, [1.5, 1.5])
    with db_param.session() as s:
        ev = s.scalar(select(Event))
        ev.status = "ACKNOWLEDGED"
    feed(db_param, [0, 0])
    with db_param.session() as s:
        assert s.scalar(select(Event)).status == "CLOSED"
