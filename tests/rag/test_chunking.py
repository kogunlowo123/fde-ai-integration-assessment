"""RAG chunking: determinism, size bounds and overlap."""

from __future__ import annotations

import pytest

from fde_assessment.rag.chunking import chunk_text, normalize

PARAGRAPHS = "\n\n".join(f"Paragraph {i} " + ("word " * 40) for i in range(10))


class TestNormalize:
    def test_collapses_horizontal_whitespace(self) -> None:
        assert normalize("a    b\tc") == "a b c"

    def test_normalises_line_endings(self) -> None:
        assert normalize("a\r\nb\rc") == "a\nb\nc"

    def test_strips_edges(self) -> None:
        assert normalize("  \n  hello  \n  ") == "hello"


class TestChunking:
    def test_small_document_is_one_chunk(self) -> None:
        assert chunk_text("A short policy statement.", 512, 64) == ["A short policy statement."]

    def test_empty_document_yields_no_chunks(self) -> None:
        assert chunk_text("", 512, 64) == []
        assert chunk_text("   \n\n  ", 512, 64) == []

    def test_chunks_respect_the_size_bound(self) -> None:
        for chunk in chunk_text(PARAGRAPHS, 300, 50):
            assert len(chunk) <= 300

    def test_output_is_deterministic(self) -> None:
        assert chunk_text(PARAGRAPHS, 300, 50) == chunk_text(PARAGRAPHS, 300, 50)

    def test_overlap_carries_context_forward(self) -> None:
        chunks = chunk_text(PARAGRAPHS, 300, 60)
        assert len(chunks) > 1
        # The tail of one chunk reappears at the head of the next.
        assert any(chunks[i][-20:] in chunks[i + 1] for i in range(len(chunks) - 1))

    def test_no_overlap_is_supported(self) -> None:
        chunks = chunk_text(PARAGRAPHS, 300, 0)
        assert all(len(chunk) <= 300 for chunk in chunks)

    def test_a_single_oversized_sentence_is_hard_split(self) -> None:
        text = "x" * 1_000
        chunks = chunk_text(text, 200, 20)
        assert len(chunks) >= 5
        assert all(len(chunk) <= 200 for chunk in chunks)

    def test_all_content_is_preserved(self) -> None:
        text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
        joined = " ".join(chunk_text(text, 40, 0))
        for marker in ("First", "Second", "Third"):
            assert marker in joined

    @pytest.mark.parametrize(("size", "overlap"), [(0, 0), (-1, 0), (100, 100), (100, 150)])
    def test_invalid_configuration_is_rejected(self, size: int, overlap: int) -> None:
        with pytest.raises(ValueError):
            chunk_text("text", size, overlap)
