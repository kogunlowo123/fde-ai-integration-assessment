"""Task 3, whole-string PII detection, Luhn validation, and limits."""

from __future__ import annotations

import pytest

from fde_assessment.llm_gateway.guardrails.pii import (
    REDACTION,
    is_card_candidate,
    luhn_check,
    partial_match_start,
    redact,
)


class TestLuhn:
    @pytest.mark.parametrize(
        "number",
        [
            "4111111111111111",  # Visa test number
            "5500005555555559",  # Mastercard test number
            "378282246310005",  # Amex test number (15 digits)
            "6011111111111117",  # Discover test number
        ],
    )
    def test_accepts_known_valid_numbers(self, number: str) -> None:
        assert luhn_check(number) is True

    @pytest.mark.parametrize("number", ["4111111111111112", "1234567890123456", "0000000000000001"])
    def test_rejects_invalid_checksums(self, number: str) -> None:
        assert luhn_check(number) is False

    @pytest.mark.parametrize("value", ["", "abcd", "4111-1111", "41111111111111 1"])
    def test_rejects_non_digits(self, value: str) -> None:
        assert luhn_check(value) is False

    def test_length_bounds_are_enforced_by_the_candidate_check(self) -> None:
        assert is_card_candidate("4111 1111 1111 1111") is True
        assert is_card_candidate("411111111111") is False  # 12 digits, too short


class TestEmail:
    @pytest.mark.parametrize(
        "email",
        [
            "john.smith@example.com",
            "a@b.co",
            "first+tag@sub.domain.example.org",
            "user_name-1%test@example-host.com",
        ],
    )
    def test_redacts_emails(self, email: str) -> None:
        result = redact(f"contact {email} today")
        assert result.text == f"contact {REDACTION} today"
        assert result.counts["email"] == 1

    @pytest.mark.parametrize("text", ["not-an-email", "@example.com", "user@", "user@host"])
    def test_leaves_non_emails_alone(self, text: str) -> None:
        assert redact(text).text == text


class TestSsn:
    def test_redacts_hyphenated_ssn(self) -> None:
        assert redact("SSN 123-45-6789 on file").text == f"SSN {REDACTION} on file"

    def test_redacts_space_separated_ssn(self) -> None:
        assert redact("SSN 123 45 6789 on file").text == f"SSN {REDACTION} on file"

    @pytest.mark.parametrize("text", ["12-345-6789", "1234-56-789", "123-456-789"])
    def test_leaves_wrong_shapes_alone(self, text: str) -> None:
        assert REDACTION not in redact(text).text

    def test_does_not_match_inside_a_longer_digit_run(self) -> None:
        assert redact("9123-45-67891").text == "9123-45-67891"


class TestCard:
    def test_redacts_spaced_card(self) -> None:
        assert redact("card 4111 1111 1111 1111 ok").text == f"card {REDACTION} ok"

    def test_redacts_hyphenated_card(self) -> None:
        assert redact("card 4111-1111-1111-1111 ok").text == f"card {REDACTION} ok"

    def test_redacts_bare_card(self) -> None:
        assert redact("card 4111111111111111 ok").text == f"card {REDACTION} ok"

    def test_redacts_amex_15_digits(self) -> None:
        assert redact("card 3782 822463 10005 ok").text == f"card {REDACTION} ok"

    def test_leaves_luhn_invalid_16_digit_numbers_alone(self) -> None:
        # The point of Luhn: not every 16-digit number is a card.
        text = "order reference 1234567890123456 shipped"
        assert redact(text).text == text

    def test_leaves_short_numbers_alone(self) -> None:
        text = "the year 2024 and code 12345"
        assert redact(text).text == text


class TestMixed:
    def test_redacts_several_values_in_one_string(self) -> None:
        result = redact("email a@b.co, ssn 123-45-6789, card 4111 1111 1111 1111, done")
        assert result.text == (f"email {REDACTION}, ssn {REDACTION}, card {REDACTION}, done")
        assert result.total == 3

    def test_is_idempotent(self) -> None:
        once = redact("mail a@b.co").text
        assert redact(once).text == once

    def test_empty_input(self) -> None:
        assert redact("").text == ""


class TestPartialMatchStart:
    @pytest.mark.parametrize(
        "text",
        ["The answer is ", "hello, world! ", "", "a sentence ending in a space "],
    )
    def test_ordinary_prose_holds_nothing(self, text: str) -> None:
        assert partial_match_start(text) == len(text)

    @pytest.mark.parametrize(
        ("text", "expected_tail"),
        [
            ("contact john.smith@", "john.smith@"),
            ("card 4111 1111", "4111 1111"),
            ("ssn 123-45", "123-45"),
            ("word", "word"),
        ],
    )
    def test_possible_prefixes_are_held(self, text: str, expected_tail: str) -> None:
        assert text[partial_match_start(text) :] == expected_tail

    def test_lookback_is_bounded(self) -> None:
        text = "x" * 5_000
        assert len(text) - partial_match_start(text, max_lookback=64) <= 64
