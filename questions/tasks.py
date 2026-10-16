"""
Celery tasks for question extraction
"""
import logging
from uuid import UUID
from celery import shared_task
from django.core.cache import cache

from questions.services.extraction_pipeline import ExtractionPipeline
from questions.services.extraction_pipeline_v2 import ExtractionPipelineV2
from questions.models import ExtractionJob

logger = logging.getLogger('extraction')


# ──────────────────────────────────────────────
# V3 extraction: Mathpix OCR + one structured Gemini call
# ──────────────────────────────────────────────

@shared_task(
    bind=True,
    name='questions.extract_questions_v3',
    max_retries=3,
    default_retry_delay=60,
)
def extract_questions_v3_task(self, job_id: str, subjects: list = None):
    """
    Extract every question in the job's file into ExtractedQuestion rows for review.

    The whole paper is OCR'd once and structured in one model call (see
    questions/services/paper_extraction.py). The model classifies questions against all
    of the pattern's subjects; `subjects` (from the UI) only filters which ones are kept.
    """
    import time
    from questions.models import ExtractionJob, ExtractedQuestion
    from questions.services.paper_extraction import PaperExtractionError, extract_questions, map_subject

    job = ExtractionJob.objects.get(id=UUID(job_id))
    job.status = 'processing'
    job.progress_percent = 10
    job.error_message = ''
    job.save(update_fields=['status', 'progress_percent', 'error_message'])
    started = time.time()

    try:
        pattern_subjects = []
        if job.pattern:
            pattern_subjects = list(dict.fromkeys(job.pattern.sections.values_list('subject', flat=True)))
        wanted = [s for s in (subjects or []) if s]
        all_subjects = list(dict.fromkeys(pattern_subjects + wanted)) or ['General']

        result = extract_questions(job.file_path, all_subjects)

        keep = {map_subject(s, all_subjects).lower() for s in wanted}
        kept = [q for q in result.questions if not keep or q['subject'].lower() in keep]

        # The pattern says which section (and so which question type) each number belongs to.
        sections = list(job.pattern.sections.all()) if job.pattern else []
        for q in kept:
            q['section'] = next((sec for sec in sections
                                 if map_subject(sec.subject, all_subjects).lower() == q['subject'].lower()
                                 and isinstance(q['number'], int)
                                 and sec.start_question <= q['number'] <= sec.end_question), None)
            if q['section'] and q['section'].question_type != q['type']:
                q['review'].append(f"pattern section '{q['section'].name}' expects {q['section'].question_type}, "
                                   f"extracted as {q['type']}")
        if not kept:
            raise PaperExtractionError(
                f'No questions for {", ".join(wanted) or "this pattern"} were found in the document '
                f'({len(result.questions)} questions found in other subjects).')

        for q in kept:
            ExtractedQuestion.objects.create(
                job=job,
                question_text=q['stem'],
                question_type=q['type'],
                options=q['options'],
                correct_answer=q['answer'],
                solution='',
                suggested_subject=q['subject'],
                suggested_section_id=q['section'].id if q['section'] else None,
                structure={},
                images_data={},
                confidence_score=0.5 if q['review'] else 0.95,
                requires_review=bool(q['review']),
                import_error='; '.join(q['review']),  # shown as the review reason in ExtractionV3/Review.tsx
            )

        logger.info(
            f"V3 extraction job {job.id}: {len(kept)}/{len(result.questions)} questions kept, "
            f"{sum(1 for q in kept if q['review'])} flagged for review, model {result.model}, "
            f"tokens {result.input_tokens}+{result.output_tokens}"
        )
        job.total_questions_found = len(result.questions)
        job.questions_extracted = len(kept)
        job.ai_model_used = result.model
        job.tokens_used = result.input_tokens + result.output_tokens
        job.save(update_fields=['total_questions_found', 'ai_model_used', 'tokens_used'])
        job.processing_time_seconds = time.time() - started
        job.mark_completed()
        return {'success': True, 'count': len(kept), 'warnings': result.warnings}

    except Exception as e:
        logger.error(f"V3 extraction job {job.id} failed: {e}", exc_info=not isinstance(e, PaperExtractionError))
        job.processing_time_seconds = time.time() - started
        job.mark_failed(str(e))
        raise


@shared_task(
    bind=True,
    name='questions.extract_questions',
    max_retries=3,
    default_retry_delay=60,  # 1 minute
    time_limit=1800,  # 30 minutes
    soft_time_limit=1700,  # 28 minutes (soft limit before hard limit)
)
def extract_questions_task(self, job_id: str, use_v2: bool = True):
    """
    Celery task to extract questions from uploaded file
    
    Args:
        job_id: UUID string of the extraction job
        use_v2: Whether to use V2 enhanced pipeline (default: True)
        
    Returns:
        Dictionary with extraction results
    """
    job_uuid = UUID(job_id)
    
    try:
        logger.info(f"Starting extraction task for job {job_id} (V2: {use_v2})")
        
        # Create pipeline and process file
        if use_v2:
            pipeline = ExtractionPipelineV2()
            result = pipeline.process_file(job_uuid)
        else:
            pipeline = ExtractionPipeline()
            pipeline.process_file(job_uuid)
            result = None
        
        # Get final job status
        job = ExtractionJob.objects.get(id=job_uuid)
        
        task_result = {
            'success': True,
            'job_id': str(job.id),
            'status': job.status,
            'questions_extracted': job.questions_extracted,
            'questions_failed': job.questions_failed,
            'expected_count': job.total_questions_found,
            'completeness': (job.questions_extracted / job.total_questions_found * 100) if job.total_questions_found > 0 else 0,
        }
        
        if result:
            task_result.update({
                'type_distribution': result.get('type_distribution', {}),
                'has_latex': result.get('has_latex', False),
                'processing_time': result.get('processing_time', 0),
            })
        
        logger.info(f"Extraction task completed for job {job_id}: {task_result}")
        return task_result
        
    except ExtractionJob.DoesNotExist:
        error_msg = f"Extraction job {job_id} not found"
        logger.error(error_msg)
        return {
            'success': False,
            'error': error_msg
        }
    
    except Exception as exc:
        logger.error(f"Extraction task failed for job {job_id}: {str(exc)}", exc_info=True)
        
        # Retry with exponential backoff
        try:
            job = ExtractionJob.objects.get(id=job_uuid)
            
            # Check if we should retry
            if job.retry_count < 3:
                # Calculate exponential backoff: 2^retry_count minutes
                countdown = (2 ** job.retry_count) * 60
                
                logger.info(
                    f"Retrying extraction job {job_id} in {countdown} seconds "
                    f"(attempt {job.retry_count + 1}/3)"
                )
                
                # Update retry count
                job.retry_count += 1
                job.save(update_fields=['retry_count'])
                
                # Retry the task
                raise self.retry(exc=exc, countdown=countdown)
            else:
                # Max retries reached
                logger.error(f"Max retries reached for job {job_id}")
                job.mark_failed(f"Max retries reached: {str(exc)}")
                
        except ExtractionJob.DoesNotExist:
            pass
        
        return {
            'success': False,
            'error': str(exc)
        }


@shared_task(
    bind=True,
    name='questions.extract_questions_v2',
    max_retries=3,
    default_retry_delay=60,
    time_limit=2400,  # 40 minutes for large files
    soft_time_limit=2300,
)
def extract_questions_v2_task(self, job_id: str):
    """
    V2 extraction task with enhanced features
    
    Features:
    - Complete extraction (100% of questions)
    - Accurate type classification
    - LaTeX preservation
    - Large file support (500+ questions)
    """
    return extract_questions_task(self, job_id, use_v2=True)


@shared_task(name='questions.cleanup_old_extraction_jobs')
def cleanup_old_extraction_jobs():
    """
    Periodic task to clean up old extraction jobs and files
    
    This should be run daily via Celery Beat
    """
    from datetime import timedelta
    from django.utils import timezone
    import os
    
    try:
        # Delete extraction jobs older than 30 days
        cutoff_date = timezone.now() - timedelta(days=30)
        
        old_jobs = ExtractionJob.objects.filter(
            created_at__lt=cutoff_date,
            status__in=['completed', 'failed']
        )
        
        deleted_count = 0
        for job in old_jobs:
            # Delete associated file if it exists
            if job.file_path and os.path.exists(job.file_path):
                try:
                    os.remove(job.file_path)
                    logger.info(f"Deleted file: {job.file_path}")
                except Exception as e:
                    logger.error(f"Failed to delete file {job.file_path}: {e}")
            
            # Delete job and related extracted questions (cascade)
            job.delete()
            deleted_count += 1
        
        logger.info(f"Cleaned up {deleted_count} old extraction jobs")
        
        return {
            'success': True,
            'deleted_count': deleted_count
        }
        
    except Exception as e:
        logger.error(f"Cleanup task failed: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e)
        }


@shared_task(
    bind=True,
    name='questions.extract_answers',
    max_retries=3,
    default_retry_delay=60,
    time_limit=900,
    soft_time_limit=840,
)
def extract_answers_task(self, job_id: str):
    """Run OCR + Gemini on an answer-key file and stage the results for review.

    Matches each extracted (question_number, answer) pair to a Question in the job's
    exam by question_number. Unmatched rows are still saved so the reviewer can see them.
    """
    from questions.models import AnswerExtractionJob, ExtractedAnswer, Question
    from questions.services.answer_key_extractor import extract_answer_key

    job_uuid = UUID(job_id)
    job = AnswerExtractionJob.objects.get(id=job_uuid)
    job.status = 'processing'
    job.progress_percent = 10
    job.save(update_fields=['status', 'progress_percent'])

    try:
        entries = extract_answer_key(job.file_path, job.file_type)
        job.progress_percent = 70
        job.save(update_fields=['progress_percent'])

        questions_by_number = {
            q.question_number: q
            for q in Question.objects.filter(exam=job.exam, is_active=True)
            if q.question_number is not None
        }

        seen_numbers = set()
        for entry in entries:
            q_no = entry['question_number']
            if q_no in seen_numbers:
                # Answer keys occasionally repeat a row (e.g. continuation pages); keep the first.
                continue
            seen_numbers.add(q_no)

            question = questions_by_number.get(q_no)
            ExtractedAnswer.objects.update_or_create(
                job=job,
                question_number=q_no,
                defaults={
                    'extracted_answer': entry['answer'],
                    'matched_question': question,
                    'current_answer': question.correct_answer if question else '',
                    'match_status': 'matched' if question else 'unmatched',
                },
            )

        job.mark_completed()
        return {'success': True, 'count': len(seen_numbers)}

    except Exception as exc:
        logger.error(f"Answer extraction failed for job {job_id}: {exc}", exc_info=True)
        job.mark_failed(str(exc))
        raise


@shared_task(name='questions.update_extraction_metrics')
def update_extraction_metrics():
    """
    Periodic task to update extraction metrics and statistics
    
    This should be run hourly via Celery Beat
    """
    try:
        from django.db.models import Count, Avg, Sum
        from datetime import timedelta
        from django.utils import timezone
        
        # Calculate metrics for last 24 hours
        since = timezone.now() - timedelta(hours=24)
        
        metrics = ExtractionJob.objects.filter(
            created_at__gte=since
        ).aggregate(
            total_jobs=Count('id'),
            completed_jobs=Count('id', filter=models.Q(status='completed')),
            failed_jobs=Count('id', filter=models.Q(status='failed')),
            avg_processing_time=Avg('processing_time_seconds'),
            total_questions_extracted=Sum('questions_extracted'),
            total_tokens_used=Sum('tokens_used'),
        )
        
        # Store in cache for dashboard
        cache.set('extraction_metrics_24h', metrics, timeout=3600)  # 1 hour
        
        logger.info(f"Updated extraction metrics: {metrics}")
        
        return {
            'success': True,
            'metrics': metrics
        }
        
    except Exception as e:
        logger.error(f"Metrics update task failed: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e)
        }
