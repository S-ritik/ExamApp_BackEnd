"""Tests for questions/services/paper_extraction.py (no database, no network)."""
import json
from unittest import mock

from django.test import SimpleTestCase, override_settings

from questions.services import paper_extraction as pe

SUBJECTS = ['Physics', 'Chemistry', 'Mathematics']


def raw(**kw):
    q = {'question_number': 1, 'subject': 'Mathematics', 'question_type': 'single_mcq',
         'question_text': 'Pick one', 'options': ['1', '2', '3', '4'], 'answer_from_source': None}
    q.update(kw)
    return q


class CleanQuestionTests(SimpleTestCase):
    def test_restores_latex_commands_decoded_as_control_characters(self):
        # json.loads('"\\frac"') is fine, but an unescaped "\frac" decodes to form feed + "rac".
        broken = json.loads('"$g(x)=\\frac{x}{2}$, $a \\times b$, $f: R \\rightarrow R$, $x \\neq 2$\\nnext"')
        fixed = pe.fix_escapes(broken)
        self.assertEqual(fixed, '$g(x)=\\frac{x}{2}$, $a \\times b$, $f: R \\rightarrow R$, $x \\neq 2$\nnext')

    def test_wraps_bare_math_options_so_they_render(self):
        q = pe.clean_question(raw(options=['\\frac{1}{2}', 'f(x)', '$x^2$', '3']), SUBJECTS, False, {})
        self.assertEqual(q['options'], ['$\\frac{1}{2}$', 'f(x)', '$x^2$', '3'])

    def test_strips_options_repeated_at_end_of_stem(self):
        q = pe.clean_question(raw(question_text='Number of solutions is (A) 1 (B) 2 (C) 3 (D) 4'), SUBJECTS, False, {})
        self.assertEqual(q['stem'], 'Number of solutions is')

    def test_keeps_stem_that_only_mentions_labels(self):
        stem = 'Assertion (A): charge is quantised. Reason (R): electrons are discrete.'
        q = pe.clean_question(raw(question_text=stem, options=['Both true', 'A true', 'R true', 'Both false']),
                              SUBJECTS, False, {})
        self.assertEqual(q['stem'], stem)

    def test_drops_answers_when_paper_prints_none(self):
        q = pe.clean_question(raw(answer_from_source='B'), SUBJECTS, False, {1: 'B'})
        self.assertEqual(q['answer'], '')

    def test_uses_answer_key_when_printed(self):
        q = pe.clean_question(raw(answer_from_source=None), SUBJECTS, True, {1: '(b)'})
        self.assertEqual(q['answer'], 'B')

    def test_maps_subject_alias_to_pattern_name(self):
        self.assertEqual(pe.clean_question(raw(subject='Math'), SUBJECTS, False, {})['subject'], 'Mathematics')

    def test_sub_parts_are_appended_to_stem(self):
        q = pe.clean_question(raw(question_type='subjective', options=[], question_text='A tile is 6 cm wide.',
                                  sub_parts=['Find the radius.', 'Find the area.']), SUBJECTS, False, {})
        self.assertEqual(q['stem'], 'A tile is 6 cm wide.\n(a) Find the radius.\n(b) Find the area.')

    def test_unknown_type_falls_back_to_subjective(self):
        q = pe.clean_question(raw(question_type='descriptive', options=[]), SUBJECTS, False, {})
        self.assertEqual(q['type'], 'subjective')

    def test_prints_answers_ignores_the_word_solution(self):
        self.assertFalse(pe.prints_answers('Number of solution(s) of the equation is'))
        self.assertTrue(pe.prints_answers('ANSWER KEY\n1. A\n2. B'))
        self.assertTrue(pe.prints_answers('Ans: (C)'))


class ReviewReasonTests(SimpleTestCase):
    def q(self, **kw):
        base = {'number': 1, 'subject': 'Mathematics', 'type': 'single_mcq', 'stem': 'Value of $x^2$ at 3',
                'options': ['9', '6', '3', '1'], 'answer': '', 'review': []}
        base.update(kw)
        return base

    def test_clean_question_has_no_reasons(self):
        self.assertEqual(pe.review_reasons(self.q(), 'Value of $x^{2}$ at 3 (A) 9 (B) 6 (C) 3 (D) 1'), [])

    def test_numeric_options_one_to_four_are_not_pointers(self):
        q = self.q(stem='Number of solutions', options=['1', '2', '3', '4'])
        self.assertEqual(pe.review_reasons(q, 'Number of solutions (A) 1 (B) 2 (C) 3 (D) 4'), [])

    def test_letter_only_options_are_flagged(self):
        reasons = pe.review_reasons(self.q(options=['A', 'B', 'C', 'D']), 'Value of x^2 at 3')
        self.assertIn('options are only labels (A, B, C, D); the real options may be in an image', reasons)

    def test_bracketed_letter_options_are_flagged(self):
        reasons = pe.review_reasons(self.q(options=['(A)', '(B)', '(C)', '(D)']), 'Value of x^2 at 3')
        self.assertIn('options are only labels (A, B, C, D); the real options may be in an image', reasons)

    def test_number_missing_from_ocr_is_flagged(self):
        reasons = pe.review_reasons(self.q(options=['9', '6', '3', '12']), 'Value of x^2 at 3 (A) 9 (B) 6 (C) 3 (D) 1')
        self.assertIn('numbers not found in the OCR text: 12', reasons)

    def test_unrenderable_math_is_flagged(self):
        reasons = pe.review_reasons(self.q(stem='Value of x^{2} at 3'), 'Value of x^{2} at 3 9 6 3 1')
        self.assertIn('some math is outside $...$ and will not render', reasons)

    def test_answer_letter_beyond_options_is_flagged(self):
        reasons = pe.review_reasons(self.q(options=['9', '6'], answer='C'), 'Value of x^2 at 3 9 6')
        self.assertIn('answer C does not match the 2 options', reasons)

    def test_duplicate_numbers_within_a_subject_are_flagged(self):
        qs = [self.q(number=1), self.q(number=1), self.q(number=1, subject='Physics')]
        warnings = pe._numbering_warnings(qs)
        self.assertEqual(warnings, ['Mathematics: duplicate question numbers [1]'])
        self.assertEqual([q['review'] for q in qs], [['duplicate question number']] * 2 + [[]])


def gemini_response(payload, finish='STOP', status=200):
    resp = mock.Mock(status_code=status, content=b'x')
    resp.json.return_value = {
        'candidates': [{'finishReason': finish, 'content': {'parts': [{'text': json.dumps(payload)}]}}],
        'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 50},
    }
    return resp


@override_settings(EXTRACTION_GEMINI_MODEL='gemini-test', EXTRACTION_GEMINI_THINKING_LEVEL='minimal',
                   GEMINI_API_KEY='k')
class ExtractQuestionsTests(SimpleTestCase):
    OCR = '1. Value of $x^{2}$ at 3\n(A) 9\n(B) 6\n(C) 3\n(D) 1\n\nAnswer Key\n1. A'

    def run_extract(self, payload, **resp_kw):
        with mock.patch.object(pe, 'ocr_document', return_value=self.OCR), \
                mock.patch.object(pe.requests, 'post', return_value=gemini_response(payload, **resp_kw)) as post:
            return pe.extract_questions('paper.pdf', SUBJECTS), post

    def test_end_to_end(self):
        payload = {'questions': [raw(question_text='Value of $x^2$ at 3', options=['9', '6', '3', '1'])],
                   'answer_key': [{'question_number': 1, 'answer': 'A'}]}
        result, post = self.run_extract(payload)
        q = result.questions[0]
        self.assertEqual((q['stem'], q['answer'], q['review']), ('Value of $x^2$ at 3', 'A', []))
        self.assertEqual((result.model, result.input_tokens, result.output_tokens), ('gemini-test', 100, 50))
        body = post.call_args.kwargs['json']
        self.assertEqual(body['generationConfig']['responseJsonSchema'], pe.SCHEMA)
        self.assertNotIn('key', post.call_args.kwargs.get('params') or {})  # key goes in a header, not the URL

    def test_truncated_output_fails_loudly(self):
        with self.assertRaisesMessage(pe.PaperExtractionError, 'MAX_TOKENS'):
            self.run_extract({'questions': []}, finish='MAX_TOKENS')

    def test_http_error_message_is_surfaced(self):
        resp = mock.Mock(status_code=404, content=b'x')
        resp.json.return_value = {'error': {'message': 'model not found'}}
        with mock.patch.object(pe, 'ocr_document', return_value=self.OCR), \
                mock.patch.object(pe.requests, 'post', return_value=resp):
            with self.assertRaisesMessage(pe.PaperExtractionError, 'HTTP 404: model not found'):
                pe.extract_questions('paper.pdf', SUBJECTS)

    def test_answer_key_entries_without_questions_are_reported(self):
        payload = {'questions': [raw(question_text='Value of $x^2$ at 3', options=['9', '6', '3', '1'])],
                   'answer_key': [{'question_number': 1, 'answer': 'A'}, {'question_number': 2, 'answer': 'C'}]}
        result, _ = self.run_extract(payload)
        self.assertIn('answer key lists questions that were not extracted: [2]', result.warnings)

    def test_empty_ocr_fails_before_calling_gemini(self):
        with mock.patch.object(pe, 'ocr_document', return_value='  '), mock.patch.object(pe.requests, 'post') as post:
            with self.assertRaises(pe.PaperExtractionError):
                pe.extract_questions('paper.pdf', SUBJECTS)
        post.assert_not_called()


class V3TaskTests(SimpleTestCase):
    """extract_questions_v3_task with the ORM and the extraction service mocked out."""

    def setUp(self):
        from questions.models import ExtractionJob, ExtractedQuestion
        self.job = mock.Mock(id='job-1', file_path='paper.pdf')
        mcq = mock.Mock(id=11, subject='Physics', start_question=1, end_question=20, question_type='single_mcq')
        num = mock.Mock(id=12, subject='Physics', start_question=21, end_question=25, question_type='numerical')
        mcq.name, num.name = 'Physics MCQ', 'Physics Numerical'
        self.job.pattern.sections.values_list.return_value = ['Physics', 'Physics', 'Chemistry']
        self.job.pattern.sections.all.return_value = [mcq, num]
        jobs = mock.patch.object(ExtractionJob, 'objects')
        self.created = mock.patch.object(ExtractedQuestion, 'objects').start().create
        jobs.start().get.return_value = self.job
        self.addCleanup(mock.patch.stopall)

    def q(self, number, subject, qtype):
        return {'number': number, 'subject': subject, 'type': qtype, 'stem': f'Q{number}', 'options': [],
                'answer': '', 'review': []}

    def test_keeps_selected_subjects_and_checks_types_against_the_pattern(self):
        from questions.tasks import extract_questions_v3_task
        result = pe.ExtractionResult(questions=[self.q(1, 'Physics', 'single_mcq'), self.q(21, 'Physics', 'subjective'),
                                                self.q(1, 'Chemistry', 'single_mcq')],
                                     model='gemini-test', input_tokens=10, output_tokens=5)
        with mock.patch.object(pe, 'extract_questions', return_value=result) as extract:
            out = extract_questions_v3_task('00000000-0000-0000-0000-000000000001', subjects=['Physics'])

        self.assertEqual(extract.call_args.args[1], ['Physics', 'Chemistry'])  # classify against every subject
        self.assertEqual(out['count'], 2)
        saved = [c.kwargs for c in self.created.call_args_list]
        self.assertEqual([(s['question_text'], s['suggested_section_id'], s['requires_review']) for s in saved],
                         [('Q1', 11, False), ('Q21', 12, True)])
        self.assertEqual(saved[1]['import_error'],
                         "pattern section 'Physics Numerical' expects numerical, extracted as subjective")
        self.assertEqual((self.job.total_questions_found, self.job.questions_extracted, self.job.tokens_used), (3, 2, 15))
        self.job.mark_completed.assert_called_once()

    def test_failure_is_recorded_on_the_job(self):
        from questions.tasks import extract_questions_v3_task
        with mock.patch.object(pe, 'extract_questions', side_effect=pe.PaperExtractionError('OCR found no text')):
            with self.assertRaises(pe.PaperExtractionError):
                extract_questions_v3_task('00000000-0000-0000-0000-000000000001')
        self.job.mark_failed.assert_called_once_with('OCR found no text')
        self.created.assert_not_called()

    def test_no_question_for_selected_subject_fails_with_a_clear_message(self):
        from questions.tasks import extract_questions_v3_task
        result = pe.ExtractionResult(questions=[self.q(1, 'Chemistry', 'single_mcq')], model='m')
        with mock.patch.object(pe, 'extract_questions', return_value=result):
            with self.assertRaises(pe.PaperExtractionError):
                extract_questions_v3_task('00000000-0000-0000-0000-000000000001', subjects=['Physics'])
        self.assertIn('No questions for Physics', self.job.mark_failed.call_args.args[0])
