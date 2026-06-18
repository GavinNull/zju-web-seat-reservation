import unittest
from datetime import date, datetime

from seat_assistant.domain import (
    ReservationConfig,
    ReservationOutcome,
    SeatRule,
    TaskState,
    choose_seat,
    classify_result_message,
    validate_transition,
)


class ReservationConfigTests(unittest.TestCase):
    def test_requires_stop_after_start_and_at_least_one_rule(self) -> None:
        with self.assertRaisesRegex(ValueError, "stop time"):
            ReservationConfig(
                name="Morning",
                venue="基础馆",
                floor="负一层",
                area="负一层书库",
                reservation_date=date(2026, 6, 15),
                time_slot="08:00-22:00",
                starts_at=datetime(2026, 6, 14, 8, 0),
                stops_at=datetime(2026, 6, 14, 8, 0),
                seat_rules=(),
            )

    def test_chooses_by_rule_priority_include_exclude_and_order(self) -> None:
        rules = (
            SeatRule(priority=2, start=1, end=20),
            SeatRule(
                priority=1,
                start=80,
                end=100,
                included=frozenset({12}),
                excluded=frozenset({89}),
                order="desc",
            ),
        )

        self.assertEqual(choose_seat({12, 89, 91, 95}, rules), 95)


class ReservationResultTests(unittest.TestCase):
    def test_classifies_chinese_result_messages(self) -> None:
        self.assertEqual(
            classify_result_message("预约成功"),
            ReservationOutcome.SUCCESS,
        )
        self.assertEqual(
            classify_result_message("当前用户在该时段已有预约，不可重复预约"),
            ReservationOutcome.ALREADY_RESERVED,
        )
        self.assertEqual(
            classify_result_message("当前区域暂无空闲座位"),
            ReservationOutcome.NO_SEAT,
        )

    def test_existing_reservation_is_terminal(self) -> None:
        outcome = ReservationOutcome.ALREADY_RESERVED
        self.assertFalse(outcome.retryable)
        self.assertEqual(outcome.task_state, TaskState.STOPPED)


class TaskStateTests(unittest.TestCase):
    def test_allows_expected_execution_transitions(self) -> None:
        validate_transition(TaskState.SCHEDULED, TaskState.RUNNING)
        validate_transition(TaskState.RUNNING, TaskState.SUBMITTING)
        validate_transition(TaskState.SUBMITTING, TaskState.VERIFYING)
        validate_transition(TaskState.VERIFYING, TaskState.SUCCEEDED)

    def test_rejects_transition_from_terminal_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid task state transition"):
            validate_transition(TaskState.SUCCEEDED, TaskState.RUNNING)


if __name__ == "__main__":
    unittest.main()
