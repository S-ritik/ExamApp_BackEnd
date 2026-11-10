"""Regression tests for question-paper PDF image rendering.

Extracted questions store Mathpix figures as BARE urls in question_text
(the exam UI's LaTeXRenderer detects them by regex). The PDF must embed
those images instead of printing the url as text.
"""
import re
import zlib
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from PIL import Image as PILImage

from exams.pdf_utils import generate_question_paper_pdf

MATHPIX_URL = (
    "https://cdn.mathpix.com/cropped/4ce0fd53-fe76-45d3-b901-4d8332e08e9d-05.jpg"
    "?height=148&width=595&top_left_y=1466&top_left_x=261"
)


class _FakeQS(list):
    def filter(self, **kwargs):
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return self


def _png_bytes():
    buf = BytesIO()
    PILImage.new("RGB", (595, 148), "red").save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_exam(question_text, options=None, question_type="subjective"):
    q = SimpleNamespace(
        question_number=21,
        question_text=question_text,
        question_type=question_type,
        options=options or [],
        structure={},
    )
    section = SimpleNamespace(
        name="Physics", subject="Physics", start_question=1, end_question=30,
        marks_per_question=4, question_configurations={},
    )
    return SimpleNamespace(
        institute=SimpleNamespace(name="Inst"),
        title="Test",
        duration_minutes=60,
        total_marks=4,
        pattern=SimpleNamespace(sections=_FakeQS([section])),
        questions=_FakeQS([q]),
    )


def _pdf_streams(pdf: bytes) -> bytes:
    """Concatenate all (decompressed) content streams of a ReportLab PDF."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.DOTALL):
        raw = m.group(1)
        try:
            out.append(zlib.decompress(raw))
        except zlib.error:
            out.append(raw)
    return b"\n".join(out)


class QuestionPaperImageTests(SimpleTestCase):
    def _render(self, exam):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda *a, **k: _FakeResponse(_png_bytes()),
        ) as urlopen:
            pdf = generate_question_paper_pdf(exam).getvalue()
        return pdf, _pdf_streams(pdf), urlopen

    def _assert_image_embedded(self, pdf, streams, urlopen):
        self.assertTrue(urlopen.called, "image url was never downloaded")
        self.assertIn(b"/Subtype /Image", pdf, "no image XObject in PDF")
        self.assertNotIn(b"mathpix", streams, "url leaked into PDF text")

    def test_bare_mathpix_url_in_question_is_embedded_as_image(self):
        text = (
            "Three charges are positioned in a straight line as depicted in the "
            f"diagram. Determine the charge Q_1. {MATHPIX_URL}"
        )
        self._assert_image_embedded(*self._render(_fake_exam(text)))

    def test_markdown_image_in_question_still_embedded(self):
        text = f"See figure. ![](https://cdn.mathpix.com/cropped/x.jpg?height=10&width=20)"
        self._assert_image_embedded(*self._render(_fake_exam(text)))

    def test_bare_url_in_mcq_option_is_embedded_as_image(self):
        exam = _fake_exam(
            "Which graph is correct?",
            options=[MATHPIX_URL, "B", "C", "D"],
            question_type="single_mcq",
        )
        self._assert_image_embedded(*self._render(exam))
