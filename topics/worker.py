import time

from django.db import transaction
from django.utils import timezone

from .models import TopicAuditEvent, TopicJob
from .runtime import AnalysisRuntime, TopicConfigurationError, TopicRuntimeError


ACTIVE_STATUSES = (
    TopicJob.Status.VALIDATING,
    TopicJob.Status.BASE_RUNNING,
    TopicJob.Status.BASE_VERIFYING,
    TopicJob.Status.QUEUED,
    TopicJob.Status.INTEREST_RUNNING,
    TopicJob.Status.INTEREST_VERIFYING,
    TopicJob.Status.PUBLISHING,
)


def _audit(job: TopicJob, action: str, details: dict | None = None) -> None:
    TopicAuditEvent.objects.create(job=job, action=action, details=details or {})


def _notify(job: TopicJob) -> None:
    from .notifications import NotificationError, notify_job

    try:
        notify_job(job)
    except NotificationError:
        _audit(job, "notification_failed")


def _save_state(job: TopicJob, status: str, progress: int, **values) -> None:
    for field, value in values.items():
        setattr(job, field, value)
    job.status = status
    job.progress = progress
    fields = [*values, "status", "progress", "updated_at"]
    job.save(update_fields=fields)


def claim_next_job() -> TopicJob | None:
    return (
        TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, status__in=ACTIVE_STATUSES)
        .exclude(runtime_state__has_key="interestCandidateStaging")
        .order_by("created_at", "id")
        .first()
    )


def _fail(job: TopicJob, error: TopicRuntimeError) -> None:
    message = str(error).strip()[:240] or "分析任务失败。"
    _save_state(
        job,
        TopicJob.Status.FAILED,
        job.progress,
        error_code=getattr(error, "code", "analysis_failed"),
        error_message=message,
        next_attempt_at=None,
        finished_at=timezone.now(),
    )
    _audit(job, "job_failed", {"errorCode": job.error_code})
    _notify(job)


def process_job(
    job_id,
    *,
    runtime: AnalysisRuntime | None = None,
    publisher=None,
) -> TopicJob:
    initial = TopicJob.objects.get(id=job_id)
    if initial.pipeline != TopicJob.Pipeline.INTEREST_V1:
        raise TopicConfigurationError("旧话题流程已停用，历史任务仅保留归档。")
    if "interestCandidateStaging" in initial.runtime_state:
        raise TopicConfigurationError("候选审核包不能作为上传分析任务运行。")
    runtime = runtime or AnalysisRuntime()

    while True:
        job = TopicJob.objects.get(id=job_id)
        try:
            if job.status == TopicJob.Status.VALIDATING:
                state = dict(job.runtime_state)
                if "inputPath" not in state:
                    state.update(runtime.validate_upload(job))
                _save_state(
                    job,
                    TopicJob.Status.BASE_RUNNING,
                    10,
                    runtime_state=state,
                    input_sha256=str(state["inputSha256"]),
                    started_at=job.started_at or timezone.now(),
                    error_code="",
                    error_message="",
                )
                _audit(job, "input_validated", {"rowCount": state.get("rowCount", 0)})
                continue

            if job.status == TopicJob.Status.BASE_RUNNING:
                state = dict(job.runtime_state)
                if "base" not in state:
                    state["base"] = runtime.run_base(job)
                _save_state(job, TopicJob.Status.BASE_VERIFYING, 30, runtime_state=state)
                _audit(job, "base_analysis_completed")
                continue

            if job.status == TopicJob.Status.BASE_VERIFYING:
                runtime.verify_base(job)
                _audit(job, "base_verification_completed")
                state = dict(job.runtime_state)
                state["interestRegistry"] = runtime.freeze_interest(job)
                _save_state(job, TopicJob.Status.INTEREST_RUNNING, 45, runtime_state=state)
                continue

            if job.status == TopicJob.Status.INTEREST_RUNNING:
                state = dict(job.runtime_state)
                state["interest"] = runtime.run_interest(job)
                _save_state(job, TopicJob.Status.INTEREST_VERIFYING, 85, runtime_state=state)
                _audit(job, "interest_analysis_completed", {
                    "candidateDegraded": bool(state["interest"].get("candidateDegraded")),
                    "candidateFailureCode": state["interest"].get("candidateFailureCode"),
                })
                continue

            if job.status == TopicJob.Status.INTEREST_VERIFYING:
                runtime.verify_interest(job)
                _audit(job, "interest_verification_completed")
                state = dict(job.runtime_state)
                state["interestVerified"] = True
                _save_state(job, TopicJob.Status.PUBLISHING, 92, runtime_state=state)
                continue

            if job.status == TopicJob.Status.QUEUED:
                state = job.runtime_state
                next_status = (
                    TopicJob.Status.INTEREST_VERIFYING if "interest" in state
                    else TopicJob.Status.INTEREST_RUNNING if "interestRegistry" in state
                    else TopicJob.Status.BASE_VERIFYING if "base" in state
                    else TopicJob.Status.VALIDATING
                )
                _save_state(job, next_status, job.progress)
                continue

            if job.status == TopicJob.Status.PUBLISHING:
                if publisher is None:
                    from publisher.interest_snapshot import publish_interest_job

                    publisher = publish_interest_job
                try:
                    publisher(job)
                except (OSError, ValueError) as exc:
                    error = TopicRuntimeError("最终产物发布失败。")
                    error.code = "publish_failed"
                    raise error from exc
                _save_state(
                    job,
                    TopicJob.Status.COMPLETED,
                    100,
                    finished_at=timezone.now(),
                    error_code="",
                    error_message="",
                    next_attempt_at=None,
                )
                _audit(job, "job_completed")
                _notify(job)
                return job

            return job
        except TopicRuntimeError as error:
            _fail(job, error)
            return job


def claim_next_backfill():
    from interests.models import InterestBackfillRequest

    return InterestBackfillRequest.objects.filter(
        status=InterestBackfillRequest.Status.PENDING
    ).order_by("created_at", "id").first()


def process_backfill_request(request_id, *, runtime: AnalysisRuntime | None = None, publisher=None):
    import json

    from interest_engine.contracts import sha256_file
    from interests.models import InterestBackfillRequest, InterestSnapshot
    from interests.registry import REGISTRY_VERSION, registry_payload
    from publisher.interest_snapshot import publish_interest_job
    from .storage import safe_job_path

    runtime = runtime or AnalysisRuntime()
    publisher = publisher or publish_interest_job
    with transaction.atomic():
        request = InterestBackfillRequest.objects.select_for_update().get(id=request_id)
        if request.status != InterestBackfillRequest.Status.PENDING:
            return request
        request.status = InterestBackfillRequest.Status.RUNNING
        request.save(update_fields=["status"])
    try:
        snapshots = list(
            InterestSnapshot.objects.select_related("job").filter(
                product_version=request.product_version,
                data_date__gte=request.start_date,
                data_date__lte=request.end_date,
            ).order_by("data_date")
        )
        payload = registry_payload()
        for snapshot in snapshots:
            job = snapshot.job
            base_dir = safe_job_path(
                job.id,
                f"interest-backfill/{request.id}/{snapshot.data_date.isoformat()}",
                create_parent=True,
            )
            base_dir.mkdir(parents=True, exist_ok=True)
            registry_path = base_dir / "entity-registry.json"
            registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            registry_hash = sha256_file(registry_path)
            output_dir = base_dir / "output"
            interest, frozen = runtime.run_interest_backfill(
                job,
                registry_path=registry_path,
                registry_version=REGISTRY_VERSION,
                registry_sha256=registry_hash,
                output_dir=output_dir,
            )
            publisher(
                job,
                interest_state=interest,
                frozen_state=frozen,
                require_publishing=False,
                replace_candidates=False,
            )
            _audit(job, "interest_entity_backfilled", {
                "requestId": str(request.id),
                "entityRegistryId": request.entity.registry_id,
                "registrySha256": registry_hash,
            })
        request.status = InterestBackfillRequest.Status.COMPLETED
        request.finished_at = timezone.now()
        request.error_code = ""
        request.save(update_fields=["status", "finished_at", "error_code"])
    except (OSError, ValueError, TopicRuntimeError) as exc:
        request.status = InterestBackfillRequest.Status.FAILED
        request.finished_at = timezone.now()
        request.error_code = getattr(exc, "code", "backfill_failed")
        request.save(update_fields=["status", "finished_at", "error_code"])
    return request


def run_worker(*, once: bool = False, poll_seconds: float = 5.0) -> None:
    while True:
        job = claim_next_job()
        if job is not None:
            process_job(job.id)
        else:
            backfill = claim_next_backfill()
            if backfill is not None:
                process_backfill_request(backfill.id)
        if once:
            return
        if job is None and backfill is None:
            time.sleep(poll_seconds)
