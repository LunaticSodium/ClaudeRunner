"""
Tests for the model_schedule / marathon_mode additions to project.py.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from claude_runner.project import (
    ModelAction,
    ModelSchedule,
    PhaseRule,
    ProjectBook,
    Trigger,
    load_project_book,
)


# ---------------------------------------------------------------------------
# ModelAction
# ---------------------------------------------------------------------------

class TestModelAction:
    def test_valid_minimal(self):
        a = ModelAction(model_id="claude-haiku-4-5-20251001")
        assert a.model_id == "claude-haiku-4-5-20251001"
        assert a.message is None

    def test_valid_with_message(self):
        a = ModelAction(model_id="x", message="switch")
        assert a.message == "switch"

    def test_missing_model_id_raises(self):
        with pytest.raises(ValidationError):
            ModelAction()


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

class TestTrigger:
    def test_all_none_is_valid(self):
        t = Trigger()
        assert t.phase_gte is None

    def test_phase_range_valid(self):
        t = Trigger(phase_gte=1, phase_lte=5)
        assert t.phase_gte == 1
        assert t.phase_lte == 5

    def test_negative_phase_rejected(self):
        with pytest.raises(ValidationError):
            Trigger(phase_gte=-1)

    def test_token_pct_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            Trigger(token_pct_gte=1.5)
        with pytest.raises(ValidationError):
            Trigger(token_pct_lte=-0.1)

    def test_matches_logic(self):
        t = Trigger(phase_gte=2, token_pct_gte=0.5)
        assert t.matches(phase=2, token_pct=0.5) is True
        assert t.matches(phase=1, token_pct=0.5) is False
        assert t.matches(phase=2, token_pct=0.4) is False


# ---------------------------------------------------------------------------
# PhaseRule
# ---------------------------------------------------------------------------

class TestPhaseRule:
    def test_valid(self):
        rule = PhaseRule(
            triggers=[Trigger(phase_gte=1)],
            action=ModelAction(model_id="haiku"),
        )
        assert len(rule.triggers) == 1

    def test_empty_triggers_rejected(self):
        with pytest.raises(ValidationError):
            PhaseRule(triggers=[], action=ModelAction(model_id="x"))


# ---------------------------------------------------------------------------
# ModelSchedule
# ---------------------------------------------------------------------------

class TestModelSchedule:
    def test_valid_minimal(self):
        sched = ModelSchedule(
            rules=[PhaseRule(triggers=[Trigger()], action=ModelAction(model_id="m"))],
        )
        assert sched.poll_interval_seconds == 15.0

    def test_custom_poll_interval(self):
        sched = ModelSchedule(
            rules=[PhaseRule(triggers=[Trigger()], action=ModelAction(model_id="m"))],
            poll_interval_seconds=30.0,
        )
        assert sched.poll_interval_seconds == 30.0

    def test_zero_poll_interval_rejected(self):
        with pytest.raises(ValidationError):
            ModelSchedule(
                rules=[PhaseRule(triggers=[Trigger()], action=ModelAction(model_id="m"))],
                poll_interval_seconds=0.0,
            )

    def test_empty_rules_rejected(self):
        with pytest.raises(ValidationError):
            ModelSchedule(rules=[])


# ---------------------------------------------------------------------------
# ProjectBook.marathon_mode
# ---------------------------------------------------------------------------

class TestMarathonMode:
    def test_default_false(self):
        book = ProjectBook(name="t", prompt="p")
        assert book.marathon_mode is False

    def test_can_set_true(self):
        book = ProjectBook(name="t", prompt="p", marathon_mode=True)
        assert book.marathon_mode is True


# ---------------------------------------------------------------------------
# ProjectBook.model_schedule
# ---------------------------------------------------------------------------

class TestProjectBookModelSchedule:
    def test_default_none(self):
        book = ProjectBook(name="t", prompt="p")
        assert book.model_schedule is None

    def test_set_model_schedule(self):
        sched = ModelSchedule(
            rules=[
                PhaseRule(
                    triggers=[Trigger(phase_gte=3)],
                    action=ModelAction(model_id="claude-sonnet-4-6"),
                )
            ]
        )
        book = ProjectBook(name="t", prompt="p", model_schedule=sched)
        assert book.model_schedule is not None
        assert book.model_schedule.rules[0].action.model_id == "claude-sonnet-4-6"

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            ProjectBook(name="t", prompt="p", model_schedule={"unknown_key": 1})


# ---------------------------------------------------------------------------
# End-to-end YAML round-trip
# ---------------------------------------------------------------------------

class TestYamlRoundTrip:
    def test_full_model_schedule_yaml(self, tmp_path):
        yaml_text = textwrap.dedent("""\
            name: model-switch-test
            prompt: "Do the work."
            marathon_mode: false
            model_schedule:
              poll_interval_seconds: 20
              rules:
                - triggers:
                    - phase_gte: 1
                      phase_lte: 2
                  action:
                    model_id: claude-haiku-4-5-20251001
                    message: "Fast Haiku for early phases"
                - triggers:
                    - phase_gte: 3
                    - token_pct_gte: 0.8
                  action:
                    model_id: claude-sonnet-4-6
                    message: "Sonnet for complex phases"
        """)
        p = tmp_path / "book.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        book = load_project_book(p)

        assert book.marathon_mode is False
        sched = book.model_schedule
        assert sched is not None
        assert sched.poll_interval_seconds == 20.0
        assert len(sched.rules) == 2

        rule0 = sched.rules[0]
        assert rule0.action.model_id == "claude-haiku-4-5-20251001"
        assert rule0.action.message == "Fast Haiku for early phases"
        assert rule0.triggers[0].phase_gte == 1
        assert rule0.triggers[0].phase_lte == 2

        rule1 = sched.rules[1]
        assert rule1.action.model_id == "claude-sonnet-4-6"
        assert len(rule1.triggers) == 2
        assert rule1.triggers[1].token_pct_gte == 0.8

    def test_marathon_mode_true_yaml(self, tmp_path):
        yaml_text = textwrap.dedent("""\
            name: marathon
            prompt: "Long run."
            marathon_mode: true
        """)
        p = tmp_path / "marathon.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        book = load_project_book(p)
        assert book.marathon_mode is True
        assert book.model_schedule is None

    def test_unknown_model_schedule_field_rejected(self, tmp_path):
        yaml_text = textwrap.dedent("""\
            name: bad
            prompt: "x"
            model_schedule:
              rules:
                - triggers: []
                  action:
                    model_id: x
              bad_field: 99
        """)
        p = tmp_path / "bad.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        with pytest.raises(Exception):  # ValidationError or similar
            load_project_book(p)


# ---------------------------------------------------------------------------
# notify.on now accepts model_switch
# ---------------------------------------------------------------------------

class TestNotifyModelSwitchEvent:
    def test_model_switch_in_notify_on(self, tmp_path):
        yaml_text = textwrap.dedent("""\
            name: t
            prompt: p
            notify:
              on: [start, complete, model_switch]
              channels:
                - type: desktop
        """)
        p = tmp_path / "book.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        book = load_project_book(p)
        assert "model_switch" in book.notify.on
