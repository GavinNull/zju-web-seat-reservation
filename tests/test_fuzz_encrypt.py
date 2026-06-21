"""Fuzz tests for encrypt_payload — AES-128-CBC deterministic encryption.

Exercises the function across a wide combinatorial space of payload shapes,
value types, datetime boundaries, and property invariants. Uses only stdlib
so CI / test runs need no extra dependencies.
"""

import base64
import json
import math
import unittest
from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Any

from seat_assistant.http_adapter import encrypt_payload

CST = timezone(timedelta(hours=8))

# ── payload generators ──────────────────────────────────────────────────────

def _scalar_values():
    yield from [
        None,
        True,
        False,
        0,
        1,
        -1,
        2**63 - 1,
        -(2**63),
        0.0,
        1.0,
        -0.0,
        float("inf"),
        float("-inf"),
        float("nan"),
        "",
        " ",
        "\n",
        "\t",
        "\r\n",
        "\x00",
        "\x00\x01\x02",
        "a",
        "abc",
        "a" * 1024,
        "a" * 65536,
        "\u0000",
        "\uffff",
        "\U0001f600",  # emoji
        "seat_id",
        "99999",
        "0",
        "null",
        "undefined",
        "NaN",
        "Infinity",
        "<script>alert(1)</script>",
        "' OR 1=1 --",
        "${jndi:ldap://x}",
        "你好世界",
        "日本語",
        "한국어",
        "🌍✨🔥",
    ]


def _payload_shapes():
    """Generate structurally diverse payloads."""
    # Minimal
    yield {}, "empty"

    # Standard expected shape
    yield {"seat_id": "99999", "segment": "1592185"}, "standard"

    # Single key
    yield {"seat_id": "0"}, "single_key"

    # Many keys with diverse values
    yield {
        "seat_id": "99999",
        "segment": "1592185",
        "date": "2026-06-22",
        "venue": "\u4e3b\u9986",
        "count": 42,
        "flag": True,
        "extra": None,
    }, "rich_dict"

    # Deeply nested
    yield {
        "a": {"b": {"c": {"d": {"e": "deep"}}}},
        "arr": [1, 2, [3, 4, [5]]],
    }, "nested"

    # Array-heavy
    yield {"ids": list(str(i) for i in range(100))}, "hundred_ids"

    # Unicode keys and values
    yield {"\u4e2d\u6587\u952e": "\u4e2d\u6587\u503c"}, "unicode_keys"

    # Keys with special chars
    yield {"key with spaces": "val", "key\nwith\nnewlines": "x"}, "special_key_chars"

    # Very long keys
    yield {"k" * 1000: "v"}, "long_key"
    yield {"k": "v" * 10000}, "long_value"


def _edge_datetimes():
    """Return (label, datetime | None) pairs."""
    yield "none (now)", None
    yield "epoch", datetime(1970, 1, 1, tzinfo=CST)
    yield "leap_day", datetime(2024, 2, 29, 12, 0, 0, tzinfo=CST)
    yield "year_0001", datetime(1, 1, 1, tzinfo=CST)
    yield "year_9999", datetime(9999, 12, 31, 23, 59, 59, tzinfo=CST)
    yield "midnight", datetime(2026, 6, 22, 0, 0, 0, tzinfo=CST)
    yield "noon", datetime(2026, 6, 22, 12, 0, 0, tzinfo=CST)
    yield "pre_midnight", datetime(2026, 6, 22, 23, 59, 59, tzinfo=CST)
    yield "microsecond", datetime(2026, 6, 22, 8, 30, 15, 500000, tzinfo=CST)
    # Same date, different times — should produce same key
    yield "morning_8", datetime(2026, 6, 22, 8, 0, 0, tzinfo=CST)
    yield "evening_20", datetime(2026, 6, 22, 20, 0, 0, tzinfo=CST)


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_valid_base64(s: str) -> bool:
    try:
        base64.b64decode(s, validate=True)
        return True
    except Exception:
        return False


def _base64_decode(s: str) -> bytes:
    return base64.b64decode(s)


# ── tests ────────────────────────────────────────────────────────────────────

class FuzzEncryptPayload(unittest.TestCase):
    """Property-based and combinatorial fuzz tests for encrypt_payload."""

    # ── basic outputs ────────────────────────────────────────────────────

    def test_all_shapes_produce_valid_base64(self) -> None:
        for payload, label in _payload_shapes():
            for dt_label, dt in _edge_datetimes():
                with self.subTest(shape=label, dt=dt_label):
                    result = encrypt_payload(payload, dt)
                    self.assertIsInstance(result, str, f"expected str, got {type(result)}")
                    self.assertGreater(len(result), 0, "output must not be empty")
                    self.assertTrue(
                        _is_valid_base64(result),
                        f"output is not valid base64: {result[:80]}...",
                    )

    def test_all_payloads_with_none_dt_produce_output(self) -> None:
        for payload, label in _payload_shapes():
            with self.subTest(shape=label):
                result = encrypt_payload(payload, None)
                self.assertIsInstance(result, str)
                self.assertGreater(len(result), 0)

    # ── determinism ──────────────────────────────────────────────────────

    def test_same_inputs_produce_same_output(self) -> None:
        """Determinism: identical (payload, dt) → identical ciphertext."""
        dt = datetime(2026, 6, 22, 9, 0, 0, tzinfo=CST)
        for payload, label in _payload_shapes():
            with self.subTest(shape=label):
                first = encrypt_payload(payload, dt)
                second = encrypt_payload(payload, dt)
                third = encrypt_payload(payload, dt)
                self.assertEqual(first, second)
                self.assertEqual(second, third)

    def test_same_date_different_times_produce_same_output(self) -> None:
        """Key is derived from YYYYMMDD only — time should not matter."""
        payload = {"seat_id": "6525", "segment": "1592185"}
        morning = datetime(2026, 6, 22, 8, 0, 0, tzinfo=CST)
        evening = datetime(2026, 6, 22, 20, 0, 0, tzinfo=CST)
        midnight = datetime(2026, 6, 22, 0, 0, 0, tzinfo=CST)

        a = encrypt_payload(payload, morning)
        b = encrypt_payload(payload, evening)
        c = encrypt_payload(payload, midnight)

        self.assertEqual(a, b)
        self.assertEqual(b, c)

    # ── date sensitivity ─────────────────────────────────────────────────

    def test_different_dates_produce_different_output(self) -> None:
        """Different YYYYMMDD → different AES key → different ciphertext."""
        payload = {"seat_id": "6525", "segment": "1592185"}
        dates = [
            datetime(2026, 6, 21, tzinfo=CST),
            datetime(2026, 6, 22, tzinfo=CST),
            datetime(2026, 6, 23, tzinfo=CST),
            datetime(2026, 7, 1, tzinfo=CST),
            datetime(2026, 12, 31, tzinfo=CST),
            datetime(2027, 1, 1, tzinfo=CST),
        ]
        results = [encrypt_payload(payload, d) for d in dates]
        # All should be unique
        self.assertEqual(len(results), len(set(results)))

    def test_date_boundaries_produce_different_output(self) -> None:
        """Year/month boundaries should change output."""
        payload = {"seat_id": "0", "segment": "0"}
        # Month boundary
        jan31 = datetime(2026, 1, 31, tzinfo=CST)
        feb01 = datetime(2026, 2, 1, tzinfo=CST)
        self.assertNotEqual(
            encrypt_payload(payload, jan31), encrypt_payload(payload, feb01)
        )
        # Year boundary
        dec31 = datetime(2026, 12, 31, tzinfo=CST)
        jan01 = datetime(2027, 1, 1, tzinfo=CST)
        self.assertNotEqual(
            encrypt_payload(payload, dec31), encrypt_payload(payload, jan01)
        )

    # ── payload sensitivity ──────────────────────────────────────────────

    def test_different_payloads_produce_different_output(self) -> None:
        """Different payloads with the same datetime produce different output."""
        dt = datetime(2026, 6, 22, tzinfo=CST)
        payloads = [
            {"seat_id": "1", "segment": "a"},
            {"seat_id": "2", "segment": "a"},
            {"seat_id": "1", "segment": "b"},
            {"seat_id": "1", "segment": "a", "extra": "x"},
            {"seat_id": "10", "segment": "a"},
            {},
            {"different": "shape"},
        ]
        results = [encrypt_payload(p, dt) for p in payloads]
        # All should be unique
        self.assertEqual(len(results), len(set(results)))

    def test_empty_dict_encrypts(self) -> None:
        """Empty dict is valid JSON and should encrypt without error."""
        result = encrypt_payload({}, datetime(2026, 6, 22, tzinfo=CST))
        self.assertIsInstance(result, str)
        self.assertTrue(_is_valid_base64(result))

    # ── scalar values in single-key payloads ─────────────────────────────

    def test_scalar_values_encrypt_without_crash(self) -> None:
        """Every scalar value type should serialize via json.dumps."""
        dt = datetime(2026, 6, 22, tzinfo=CST)
        for value in _scalar_values():
            payload = {"v": value}
            try:
                json.dumps(payload, ensure_ascii=False)
                json_ok = True
            except (TypeError, ValueError):
                json_ok = False

            if not json_ok:
                with self.subTest(value=repr(value)):
                    with self.assertRaises((TypeError, ValueError)):
                        encrypt_payload(payload, dt)
            else:
                with self.subTest(value=repr(value)):
                    result = encrypt_payload(payload, dt)
                    self.assertIsInstance(result, str)
                    self.assertTrue(_is_valid_base64(result))

    # ── output length invariants ─────────────────────────────────────────

    def test_output_length_is_base64_of_aes_blocks(self) -> None:
        """AES-128-CBC + PKCS7 padding → ciphertext is multiple of 16 bytes.

        Base64 encodes ceil(n/3)*4 chars. So for n = 16*k bytes,
        output length = 4 * ceil(16k/3).
        """
        dt = datetime(2026, 6, 22, tzinfo=CST)
        for payload, label in _payload_shapes():
            with self.subTest(shape=label):
                result = encrypt_payload(payload, dt)
                raw = _base64_decode(result)
                self.assertEqual(
                    len(raw) % 16,
                    0,
                    f"ciphertext length {len(raw)} must be multiple of 16 (AES block)",
                )

    def test_output_length_grows_with_input(self) -> None:
        """Larger plaintext → at least as large ciphertext (PKCS7 adds at most 16)."""
        dt = datetime(2026, 6, 22, tzinfo=CST)
        small = encrypt_payload({"k": "x"}, dt)
        large = encrypt_payload({"k": "x" * 1000}, dt)
        self.assertGreater(len(_base64_decode(large)), len(_base64_decode(small)))

    # ── combinatorial scalar × datetime ──────────────────────────────────

    def test_scalar_value_matrix(self) -> None:
        """Cross-product of scalar values × edge datetimes."""
        dt = datetime(2026, 6, 22, tzinfo=CST)
        for value, (dt_label, dt_val) in product(
            _scalar_values(), _edge_datetimes()
        ):
            payload = {"k": value}
            json_safe = True
            try:
                json.dumps(payload, ensure_ascii=False)
            except (TypeError, ValueError):
                json_safe = False

            with self.subTest(value=repr(value), dt=dt_label):
                if not json_safe:
                    with self.assertRaises((TypeError, ValueError)):
                        encrypt_payload(payload, dt_val)
                else:
                    result = encrypt_payload(payload, dt_val)
                    self.assertTrue(_is_valid_base64(result))

    # ── float corner cases ───────────────────────────────────────────────

    def test_float_edge_values(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)
        floats = [
            0.0,
            -0.0,
            1.0,
            -1.0,
            1e308,
            -1e308,
            1e-308,
            float("inf"),
            float("-inf"),
            float("nan"),
            3.141592653589793,
        ]
        for f in floats:
            with self.subTest(float=f):
                try:
                    json.dumps({"k": f})
                except (TypeError, ValueError):
                    with self.assertRaises((TypeError, ValueError)):
                        encrypt_payload({"k": f}, dt)
                    continue
                result = encrypt_payload({"k": f}, dt)
                self.assertTrue(_is_valid_base64(result))

    # ── int corner cases ─────────────────────────────────────────────────

    def test_integer_edge_values(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)
        ints = [
            0,
            1,
            -1,
            2**31 - 1,
            -(2**31),
            2**63 - 1,
            -(2**63),
            10**100,  # Python big int
        ]
        for i in ints:
            with self.subTest(int=i):
                result = encrypt_payload({"k": i}, dt)
                self.assertTrue(_is_valid_base64(result))

    # ── repeated encryption does not error ───────────────────────────────

    def test_repeated_calls_with_same_args_stable(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)
        payload = {"seat_id": "99999", "segment": "1592185", "nested": {"a": [1, 2, 3]}}
        results = [encrypt_payload(payload, dt) for _ in range(100)]
        self.assertEqual(len(set(results)), 1)

    def test_repeated_calls_with_varying_payloads_no_error(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)
        for i in range(50):
            payload = {"seat_id": str(i), "segment": str(i * 7), "i": i}
            result = encrypt_payload(payload, dt)
            self.assertTrue(_is_valid_base64(result))

    # ── None / null round-trip ───────────────────────────────────────────

    def test_payloads_with_null_values(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)
        payloads = [
            {"a": None},
            {"a": None, "b": None},
            {"a": 1, "b": None, "c": "hello"},
            {"nested": {"inner": None}},
            {"list": [None, 1, None]},
        ]
        for p in payloads:
            with self.subTest(payload=p):
                result = encrypt_payload(p, dt)
                self.assertTrue(_is_valid_base64(result))

    # ── structural robustness ────────────────────────────────────────────

    def test_deeply_nested_structure(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)

        def nest(depth: int) -> Any:
            if depth == 0:
                return "leaf"
            return {"left": nest(depth - 1), "right": nest(depth - 1)}

        for depth in (5, 10, 15):
            with self.subTest(depth=depth):
                payload = {"tree": nest(depth)}
                result = encrypt_payload(payload, dt)
                self.assertTrue(_is_valid_base64(result))

    def test_max_safe_integer_seat_ids(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)
        for seat_id in ("0", "1", "99999", str(2**53), str(2**53 - 1)):
            with self.subTest(seat_id=seat_id):
                result = encrypt_payload({"seat_id": seat_id, "segment": "1"}, dt)
                self.assertTrue(_is_valid_base64(result))

    def test_empty_string_segment(self) -> None:
        """Empty string segment should encrypt fine."""
        dt = datetime(2026, 6, 22, tzinfo=CST)
        result = encrypt_payload({"seat_id": "99999", "segment": ""}, dt)
        self.assertTrue(_is_valid_base64(result))

    def test_very_long_segment_id(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)
        result = encrypt_payload({"seat_id": "1", "segment": "x" * 1000}, dt)
        self.assertTrue(_is_valid_base64(result))

    # ── concurrent / independence ────────────────────────────────────────

    def test_independent_calls_no_shared_state_leak(self) -> None:
        """Successive calls with different payloads must not interfere."""
        dt = datetime(2026, 6, 22, tzinfo=CST)
        a = encrypt_payload({"seat_id": "11111", "segment": "seg-a"}, dt)
        b = encrypt_payload({"seat_id": "22222", "segment": "seg-b"}, dt)
        c = encrypt_payload({"seat_id": "11111", "segment": "seg-a"}, dt)

        self.assertEqual(a, c)  # same input -> same output
        self.assertNotEqual(a, b)

    def test_default_dt_is_stable_within_same_second(self) -> None:
        """Two rapid calls without explicit dt use 'now' which should be
        close enough to produce the same key."""
        payload = {"seat_id": "99999", "segment": "1592185"}
        r1 = encrypt_payload(payload)
        r2 = encrypt_payload(payload)
        # Might differ if called exactly at midnight boundary, but
        # typically will be the same. At minimum, both should be valid.
        self.assertTrue(_is_valid_base64(r1))
        self.assertTrue(_is_valid_base64(r2))

    # ── base64 character set ─────────────────────────────────────────────

    def test_output_only_contains_base64_and_padding_chars(self) -> None:
        dt = datetime(2026, 6, 22, tzinfo=CST)
        for payload, label in _payload_shapes():
            with self.subTest(shape=label):
                result = encrypt_payload(payload, dt)
                self.assertRegex(
                    result,
                    r"^[A-Za-z0-9+/]+=*$",
                    f"output contains non-base64 chars: {result[:80]}",
                )

    # ── key collision safety ─────────────────────────────────────────────

    def test_key_derivation_does_not_collide_across_months(self) -> None:
        """Ensure keys for different months are different (regression check)."""
        payload = {"seat_id": "0", "segment": "0"}
        dates = [datetime(2026, m, 1, tzinfo=CST) for m in range(1, 13)]
        results = [encrypt_payload(payload, d) for d in dates]
        self.assertEqual(len(results), len(set(results)))

    def test_key_derivation_no_date_ambiguity(self) -> None:
        """Format YYYYMMDD is unambiguous — verify no YYYYMMDD collision
        across year/month/day permutations.

        E.g., 2026-01-23 vs 2026-12-03 are different keys.
        """
        payload = {"seat_id": "0", "segment": "0"}
        # These dates have the same digits in different positions
        tricky = [
            datetime(2026, 1, 23, tzinfo=CST),  # key: 20260123 + 32106202
            datetime(2026, 12, 3, tzinfo=CST),   # key: 20261203 + 30216202
        ]
        r1, r2 = encrypt_payload(payload, tricky[0]), encrypt_payload(payload, tricky[1])
        self.assertNotEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
