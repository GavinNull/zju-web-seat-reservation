import unittest

from seat_assistant.redaction import redact_text


class RedactionTests(unittest.TestCase):
    def test_redacts_common_secret_assignments(self) -> None:
        source = (
            "token=abc123 cookie: session-value "
            "Authorization: Bearer secret-value webhook=https://example.test/hook"
        )

        redacted = redact_text(source)

        self.assertNotIn("abc123", redacted)
        self.assertNotIn("session-value", redacted)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("https://example.test/hook", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 4)

    def test_preserves_non_sensitive_control_text(self) -> None:
        source = "Button name='提交预约' automation_id='submitButton'"

        self.assertEqual(redact_text(source), source)


if __name__ == "__main__":
    unittest.main()

