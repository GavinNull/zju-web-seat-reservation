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

    def test_allows_three_second_refresh_minimum(self) -> None:
        config = ReservationConfig(
            name="Morning",
            venue="Library",
            floor="Floor 1",
            area="Area A",
            reservation_date=date(2026, 6, 15),
            time_slot="08:00-22:00",
            starts_at=datetime(2026, 6, 14, 7, 59, 50),
            stops_at=datetime(2026, 6, 14, 8, 10),
            seat_rules=(SeatRule(priority=1, start=80, end=100),),
            refresh_min_seconds=3,
            refresh_max_seconds=5,
        )

        self.assertEqual(config.refresh_min_seconds, 3)

    def test_rejects_refresh_minimum_below_three_seconds(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 3 seconds"):
            ReservationConfig(
                name="Morning",
                venue="Library",
                floor="Floor 1",
                area="Area A",
                reservation_date=date(2026, 6, 15),
                time_slot="08:00-22:00",
                starts_at=datetime(2026, 6, 14, 7, 59, 50),
                stops_at=datetime(2026, 6, 14, 8, 10),
                seat_rules=(SeatRule(priority=1, start=80, end=100),),
                refresh_min_seconds=2.9,
                refresh_max_seconds=5,
            )


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
