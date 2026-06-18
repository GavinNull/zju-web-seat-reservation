import unittest
from datetime import datetime, timedelta

from seat_assistant.scheduler import AttemptPolicy


class AttemptPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 6, 15, 7, 59, 50)
        self.stop = datetime(2026, 6, 15, 8, 10, 0)

    def test_does_not_run_outside_time_window(self) -> None:
        policy = AttemptPolicy(self.start, self.stop, 8, 15, 20)

        self.assertFalse(policy.can_attempt(self.start - timedelta(seconds=1), 0))
        self.assertTrue(policy.can_attempt(self.start, 0))
        self.assertFalse(policy.can_attempt(self.stop, 0))

    def test_enforces_conservative_minimum_delay(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 8"):
            AttemptPolicy(self.start, self.stop, 2, 15, 20)

    def test_delay_uses_jitter_and_bounded_backoff(self) -> None:
        policy = AttemptPolicy(self.start, self.stop, 8, 15, 20)

        self.assertEqual(policy.next_delay(0, random_unit=0.0), 8)
        self.assertEqual(policy.next_delay(0, random_unit=1.0), 15)
        self.assertEqual(policy.next_delay(3, random_unit=0.0), 15)

    def test_stops_at_maximum_attempts(self) -> None:
        policy = AttemptPolicy(self.start, self.stop, 8, 15, 3)

        self.assertTrue(policy.can_attempt(self.start, 2))
        self.assertFalse(policy.can_attempt(self.start, 3))


if __name__ == "__main__":
    unittest.main()
