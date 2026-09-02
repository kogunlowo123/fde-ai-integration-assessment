"""Task 3, the chunk-boundary requirement.

PII split across stream deltas must be redacted exactly as if it had arrived in
one piece. These tests drive the redactor with every splitting of the input,
which is the only way to be confident the boundary logic has no gaps.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from fde_assessment.common.models import StreamEvent
from fde_assessment.llm_gateway.guardrails.pii import REDACTION
from fde_assessment.llm_gateway.guardrails.streaming import StreamingRedactor, guard_stream

EMAIL_TEXT = "Please contact john.smith@example.com about the order."
SSN_TEXT = "The identifier is 123-45-6789 for that account."
CARD_TEXT = "Charge the card 4111 1111 1111 1111 today."
MIXED_TEXT = "Contact john.smith@example.com, ssn 123-45-6789, card 4111-1111-1111-1111. Done."


def drive(chunks: Sequence[str], window: int = 128) -> str:
    """Feed ``chunks`` through a redactor and return the concatenated output."""
    redactor = StreamingRedactor(window)
    out = [redactor.process(chunk) for chunk in chunks]
    out.append(redactor.flush())
    return "".join(out)


def every_split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class TestSingleChunk:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (EMAIL_TEXT, f"Please contact {REDACTION} about the order."),
            (SSN_TEXT, f"The identifier is {REDACTION} for that account."),
            (CARD_TEXT, f"Charge the card {REDACTION} today."),
        ],
    )
    def test_whole_text_in_one_chunk(self, text: str, expected: str) -> None:
        assert drive([text]) == expected


class TestSplitAcrossChunks:
    def test_the_assessment_example_split_into_three(self) -> None:
        # "john.smith@" / "example." / "com", the exact case in the brief.
        assert drive(["Mail john.smith@", "example.", "com now"]) == f"Mail {REDACTION} now"

    @pytest.mark.parametrize("text", [EMAIL_TEXT, SSN_TEXT, CARD_TEXT, MIXED_TEXT])
    @pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11, 17, 64])
    def test_every_fixed_chunk_size_matches_the_single_chunk_result(
        self, text: str, size: int
    ) -> None:
        assert drive(every_split(text, size)) == drive([text])

    @pytest.mark.parametrize("text", [EMAIL_TEXT, SSN_TEXT, CARD_TEXT, MIXED_TEXT])
    def test_every_possible_two_way_split(self, text: str) -> None:
        expected = drive([text])
        for cut in range(len(text) + 1):
            assert drive([text[:cut], text[cut:]]) == expected, f"failed at cut={cut}"

    def test_character_by_character(self) -> None:
        assert drive(list(MIXED_TEXT)) == drive([MIXED_TEXT])

    def test_pii_at_the_very_end_of_the_stream(self) -> None:
        assert drive(["The address is ", "john.smith@example.com"]) == f"The address is {REDACTION}"

    def test_pii_at_the_very_start_of_the_stream(self) -> None:
        assert drive(["john.smith@example.com", " is the address"]) == f"{REDACTION} is the address"

    def test_adjacent_pii_values(self) -> None:
        assert drive(["a@b.co ", "c@d.io"]) == f"{REDACTION} {REDACTION}"


class TestDegenerateInputs:
    def test_empty_chunks_are_ignored(self) -> None:
        assert drive(["", "Mail ", "", "a@b.co", ""]) == f"Mail {REDACTION}"

    def test_empty_stream(self) -> None:
        assert drive([]) == ""

    def test_whitespace_only_stream(self) -> None:
        assert drive(["   ", "\n", "\t"]) == "   \n\t"

    def test_unicode_is_preserved(self) -> None:
        assert drive(["Grüße ", "aus Köln: ", "a@b.co"]) == f"Grüße aus Köln: {REDACTION}"

    def test_partial_pii_that_never_completes_is_emitted_verbatim(self) -> None:
        assert drive(["contact john.smith@", "example"]) == "contact john.smith@example"


class TestBoundedMemory:
    def test_carry_never_exceeds_the_window(self) -> None:
        redactor = StreamingRedactor(window=64)
        # A long unbroken token is the adversarial case: it always looks like a
        # possible email local-part, so the naive implementation would hold it
        # all. The hard cap must kick in.
        for _ in range(200):
            redactor.process("a" * 50)
            assert redactor.buffered_chars <= 64
        redactor.flush()

    def test_long_stream_does_not_accumulate(self) -> None:
        redactor = StreamingRedactor(window=128)
        emitted = 0
        for i in range(2_000):
            emitted += len(redactor.process(f"sentence number {i} of a long response. "))
            assert redactor.buffered_chars <= 128
        emitted += len(redactor.flush())
        assert emitted > 50_000  # the text really did flow through

    def test_ordinary_prose_is_emitted_without_delay(self) -> None:
        """Low TTFT: text that cannot start a match is never held back."""
        redactor = StreamingRedactor()
        assert redactor.process("The answer is ") == "The answer is "
        assert redactor.buffered_chars == 0

    def test_overlong_match_is_documented_to_be_truncated(self) -> None:
        """An email longer than the window can partially escape redaction.

        Asserted rather than hidden: this is the cost of a bounded buffer and
        is called out in the module docstring and ADR-005.
        """
        long_email = "a" * 300 + "@example.com"
        output = drive([long_email], window=64)
        assert REDACTION not in output or output != f"{REDACTION}"


class TestGuardStream:
    async def _events(self, chunks: Sequence[str]) -> AsyncIterator[StreamEvent]:
        for chunk in chunks:
            yield StreamEvent(text=chunk, completion_tokens=1)
        yield StreamEvent(done=True)

    async def _collect(self, chunks: Sequence[str]) -> tuple[str, list[StreamEvent]]:
        events = [event async for event in guard_stream(self._events(chunks))]
        return "".join(e.text for e in events if not e.done), events

    async def test_redacts_across_provider_events(self) -> None:
        text, events = await self._collect(["Mail john.smith@", "example.", "com now"])
        assert text == f"Mail {REDACTION} now"
        assert events[-1].done is True

    async def test_terminal_event_carries_token_total(self) -> None:
        _, events = await self._collect(["one ", "two ", "three"])
        assert events[-1].completion_tokens == 3

    async def test_client_disconnect_does_not_raise(self) -> None:
        """Closing the generator mid-stream must not blow up.

        The unemitted carry is discarded, which is safe: it was never scanned
        and nobody is listening for it.
        """
        stream = guard_stream(self._events(["Mail john.smith@", "example.com"]))
        first = await anext(stream)
        assert first.text == "Mail "
        await stream.aclose()  # simulates the client hanging up

    async def test_provider_error_propagates(self) -> None:
        async def failing() -> AsyncIterator[StreamEvent]:
            yield StreamEvent(text="partial ")
            raise RuntimeError("upstream died")

        with pytest.raises(RuntimeError):
            async for _ in guard_stream(failing()):
                pass
