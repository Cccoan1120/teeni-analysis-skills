import json
import os
import re
import shutil
import time
from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, HttpRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from dashboard.product_versions import DEFAULT_PRODUCT_VERSION, is_product_version
from interests.product_versions import versioned_request

from interest_engine import ENGINE_VERSION as INTEREST_ENGINE_VERSION

from .models import TopicArtifact, TopicAuditEvent, TopicJob
from .serializers import serialize_event, serialize_job
from .storage import disk_has_capacity, job_directory, safe_job_path, upload_path


SHA256 = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATUSES = {TopicJob.Status.COMPLETED, TopicJob.Status.FAILED, TopicJob.Status.EXPIRED}
RETRYABLE_ERRORS = {"model_unavailable", "worker_interrupted", "temporary_io", "analysis_timeout", "publish_failed"}
DELETE_BLOCKED_STATUSES = {
    TopicJob.Status.VALIDATING,
    TopicJob.Status.BASE_RUNNING,
    TopicJob.Status.BASE_VERIFYING,
    TopicJob.Status.SEMANTIC_RUNNING,
    TopicJob.Status.INTEREST_RUNNING,
    TopicJob.Status.INTEREST_VERIFYING,
    TopicJob.Status.FINAL_VERIFYING,
    TopicJob.Status.PUBLISHING,
}


def _json_body(request: HttpRequest) -> dict:
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid_json")
    return payload


def _error(code: str, detail: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": code, "detail": detail}, status=status)


def _storage_error() -> JsonResponse:
    return _error("storage_unavailable", "服务器上传存储暂不可用，请稍后重试。", 503)


def _audit(job: TopicJob, action: str, actor=None, details: dict | None = None) -> None:
    TopicAuditEvent.objects.create(job=job, actor=actor, action=action, details=details or {})


@require_http_methods(["GET", "POST"])
def interest_jobs(request: HttpRequest):
    return create_job(request) if request.method == "POST" else list_jobs(request)


@require_POST
@login_required
@versioned_request
def create_job(request: HttpRequest) -> JsonResponse:
    try:
        payload = _json_body(request)
        data_date = date.fromisoformat(str(payload.get("dataDate", "")))
        original_name = str(payload.get("fileName", "")).strip()
        expected_size = int(payload.get("fileSize", 0))
    except (ValueError, TypeError):
        return _error("invalid_job", "请提供有效的数据日期、CSV文件名和文件大小。")

    product_version = payload.get("productVersion", DEFAULT_PRODUCT_VERSION)
    if not is_product_version(product_version):
        return _error("invalid_product_version", "productVersion must be M1 or M2")
    if "productVersion" in request.GET and request.product_version != product_version:
        return _error("invalid_product_version", "upload version does not match selected version")

    requested_pipeline = payload.get("pipeline", TopicJob.Pipeline.INTEREST_V1)
    if requested_pipeline != TopicJob.Pipeline.INTEREST_V1:
        return _error("invalid_pipeline", "目前仅支持兴趣分析。")

    if not original_name or Path(original_name).name != original_name or not original_name.lower().endswith(".csv"):
        return _error("invalid_file_name", "仅接受不含路径的CSV文件名。")
    if expected_size < 1 or expected_size > settings.TEENI_TOPIC_MAX_UPLOAD_BYTES:
        return _error("invalid_file_size", "文件大小超出允许范围。")
    if not disk_has_capacity(expected_size):
        return _error("disk_reserve", "服务器可用空间不足20%，暂时不能接收新任务。", 507)

    pipeline = TopicJob.Pipeline(requested_pipeline)
    resumed = TopicJob.objects.filter(
        created_by=request.user,
        data_date=data_date,
        original_name=original_name,
        expected_size=expected_size,
        status=TopicJob.Status.UPLOADING,
        pipeline=pipeline,
        product_version=product_version,
    ).order_by("-created_at").first()
    if resumed:
        try:
            upload_path(resumed.id, create=True).touch(exist_ok=True)
        except OSError:
            return _storage_error()
        return JsonResponse({"job": serialize_job(resumed), "resumed": True}, status=200)

    job = None
    try:
        with transaction.atomic():
            runtime_state = {"interestEngine": INTEREST_ENGINE_VERSION}
            job = TopicJob.objects.create(
                created_by=request.user,
                data_date=data_date,
                original_name=original_name[:255],
                expected_size=expected_size,
                pipeline=pipeline,
                product_version=product_version,
                runtime_state=runtime_state,
            )
            upload_path(job.id, create=True).touch(exist_ok=False)
            _audit(job, "job_created", request.user, {
                "expectedSize": expected_size,
                "dataDate": data_date.isoformat(),
                "pipeline": pipeline.value,
            })
    except OSError:
        if job is not None:
            shutil.rmtree(job_directory(job.id), ignore_errors=True)
        return _storage_error()
    return JsonResponse(
        {"job": serialize_job(job), "chunkSize": settings.TEENI_TOPIC_CHUNK_BYTES, "resumed": False},
        status=201,
    )


@require_http_methods(["PATCH"])
@login_required
@versioned_request
def upload_chunk(request: HttpRequest, job_id) -> JsonResponse:
    try:
        offset = int(request.headers.get("Upload-Offset", ""))
    except ValueError:
        return _error("invalid_offset", "Upload-Offset 必须是非负整数。")
    chunk = request.body
    if offset < 0 or not chunk or len(chunk) > settings.TEENI_TOPIC_CHUNK_BYTES:
        return _error("invalid_chunk", "分片必须非空且不超过8 MiB。")

    try:
        with transaction.atomic():
            job = get_object_or_404(TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, product_version=request.product_version).select_for_update(), id=job_id)
            if job.status != TopicJob.Status.UPLOADING:
                return _error("upload_closed", "该任务已结束上传。", 409)
            if offset != job.received_size:
                response = _error("offset_mismatch", "上传偏移量与服务器不一致。", 409)
                response["Upload-Offset"] = str(job.received_size)
                return response
            if job.received_size + len(chunk) > job.expected_size:
                return _error("upload_overflow", "分片超过声明的文件大小。", 409)

            path = upload_path(job.id, create=True)
            with path.open("r+b") as stream:
                stream.seek(offset)
                stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            job.received_size += len(chunk)
            job.progress = min(4, int(job.received_size * 4 / job.expected_size))
            job.save(update_fields=["received_size", "progress", "updated_at"])
    except OSError:
        return _storage_error()

    response = JsonResponse({}, status=204)
    response["Upload-Offset"] = str(job.received_size)
    return response


@require_POST
@login_required
@versioned_request
def complete_upload(request: HttpRequest, job_id) -> JsonResponse:
    try:
        payload = _json_body(request)
    except ValueError:
        return _error("invalid_json", "请求正文不是有效JSON。")
    expected_hash = str(payload.get("sha256", "")).lower()
    if not SHA256.fullmatch(expected_hash):
        return _error("invalid_sha256", "sha256 必须是64位小写十六进制。")

    with transaction.atomic():
        job = get_object_or_404(TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, product_version=request.product_version).select_for_update(), id=job_id)
        if job.status != TopicJob.Status.UPLOADING:
            return _error("upload_closed", "该任务已结束上传。", 409)
        path = upload_path(job.id)
        if job.received_size != job.expected_size or not path.is_file() or path.stat().st_size != job.expected_size:
            return _error("upload_incomplete", "文件尚未完整上传。", 409)

        duplicate = TopicJob.objects.filter(
            data_date=job.data_date,
            input_sha256=expected_hash,
            pipeline=job.pipeline,
            product_version=job.product_version,
        ).exclude(id=job.id).exclude(status=TopicJob.Status.EXPIRED).order_by("-created_at").first()
        if duplicate:
            job.status = TopicJob.Status.EXPIRED
            job.error_code = "duplicate"
            job.error_message = "相同日期和文件已存在。"
            job.save(update_fields=["status", "error_code", "error_message", "updated_at"])
            _audit(job, "duplicate_reused", request.user, {"existingJobId": str(duplicate.id)})
            shutil.rmtree(job_directory(job.id), ignore_errors=True)
            return JsonResponse({"job": serialize_job(duplicate), "duplicate": True}, status=200)

        job.expected_sha256 = expected_hash
        job.status = TopicJob.Status.VALIDATING
        job.progress = 5
        job.error_code = ""
        job.error_message = ""
        job.save(update_fields=["expected_sha256", "status", "progress", "error_code", "error_message", "updated_at"])
        _audit(job, "upload_completed", request.user, {"byteSize": job.expected_size})
    return JsonResponse({"job": serialize_job(job), "duplicate": False}, status=202)


@require_GET
@login_required
@versioned_request
def list_jobs(request: HttpRequest) -> JsonResponse:
    jobs = (
        TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, product_version=request.product_version).select_related("created_by")
        .prefetch_related("artifacts")
        .exclude(status=TopicJob.Status.EXPIRED)[:100]
    )
    return JsonResponse({"items": [serialize_job(job) for job in jobs]})


@require_http_methods(["GET", "DELETE"])
@login_required
@versioned_request
def job_detail(request: HttpRequest, job_id) -> JsonResponse:
    if request.method == "DELETE":
        return delete_job(request, job_id)
    job = get_object_or_404(
        TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, product_version=request.product_version).select_related("created_by")
        .prefetch_related("artifacts", "events__actor")
        .exclude(status=TopicJob.Status.EXPIRED),
        id=job_id,
    )
    return JsonResponse({"job": serialize_job(job, include_events=True)})


def delete_job(request: HttpRequest, job_id) -> JsonResponse:
    with transaction.atomic():
        job = get_object_or_404(
            TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, product_version=request.product_version).select_for_update().exclude(status=TopicJob.Status.EXPIRED),
            id=job_id,
        )
        if job.status in DELETE_BLOCKED_STATUSES:
            return _error("job_active", "任务正在处理，完成或暂停后才能删除。", 409)
        previous_status = job.status
        directory = job_directory(job.id)
        if directory.exists():
            shutil.rmtree(directory)
        job.artifacts.all().delete()
        job.status = TopicJob.Status.EXPIRED
        job.runtime_state = {}
        job.error_code = "user_deleted"
        job.error_message = "任务已由用户删除。"
        job.next_attempt_at = None
        job.finished_at = timezone.now()
        job.save(update_fields=[
            "status", "runtime_state", "error_code", "error_message", "next_attempt_at",
            "finished_at", "updated_at",
        ])
        _audit(job, "job_deleted", request.user, {"previousStatus": previous_status})
    return JsonResponse({}, status=204)


@require_GET
@login_required
@versioned_request
def job_events(request: HttpRequest, job_id) -> StreamingHttpResponse:
    get_object_or_404(TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, product_version=request.product_version).exclude(status=TopicJob.Status.EXPIRED), id=job_id)
    try:
        after = max(0, int(request.headers.get("Last-Event-ID", request.GET.get("after", "0"))))
    except ValueError:
        after = 0

    def stream():
        cursor = after
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            events = TopicAuditEvent.objects.filter(job_id=job_id, id__gt=cursor).select_related("actor")[:50]
            for event in events:
                cursor = event.id
                yield f"id: {event.id}\nevent: audit\ndata: {json.dumps(serialize_event(event), ensure_ascii=False)}\n\n"
            job = TopicJob.objects.get(id=job_id)
            state = {"status": job.status, "progress": job.progress, "updatedAt": job.updated_at.isoformat()}
            yield f"event: state\ndata: {json.dumps(state, ensure_ascii=False)}\n\n"
            if job.status in TERMINAL_STATUSES:
                return
            time.sleep(1)

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response


@require_POST
@login_required
@versioned_request
def retry_job(request: HttpRequest, job_id) -> JsonResponse:
    with transaction.atomic():
        job = get_object_or_404(
            TopicJob.objects.filter(pipeline=TopicJob.Pipeline.INTEREST_V1, product_version=request.product_version).select_for_update().exclude(status=TopicJob.Status.EXPIRED),
            id=job_id,
        )
        if job.status in {TopicJob.Status.WAITING_MODEL, TopicJob.Status.FAILED} and (
            job.status == TopicJob.Status.WAITING_MODEL or job.error_code in RETRYABLE_ERRORS
        ):
            if job.error_code == "publish_failed" and job.runtime_state.get("interestVerified"):
                next_status = TopicJob.Status.PUBLISHING
            elif "base" not in job.runtime_state:
                next_status = TopicJob.Status.VALIDATING
            elif "interest" in job.runtime_state:
                next_status = TopicJob.Status.INTEREST_VERIFYING
            elif "interestRegistry" in job.runtime_state:
                next_status = TopicJob.Status.INTEREST_RUNNING
            else:
                next_status = TopicJob.Status.BASE_VERIFYING
        else:
            return _error("retry_not_allowed", "该失败状态不能直接重试。", 409)
        job.status = next_status
        job.retry_count += 1
        job.next_attempt_at = None
        job.error_code = ""
        job.error_message = ""
        job.save(update_fields=["status", "retry_count", "next_attempt_at", "error_code", "error_message", "updated_at"])
        _audit(job, "job_retried", request.user, {"retryCount": job.retry_count})
    return JsonResponse({"job": serialize_job(job)})


@require_GET
@login_required
@versioned_request
def download_artifact(request: HttpRequest, job_id, artifact_id):
    artifact = get_object_or_404(
        TopicArtifact.objects.filter(job__pipeline=TopicJob.Pipeline.INTEREST_V1, job__product_version=request.product_version, kind__in=[TopicArtifact.Kind.INTEREST_WORKBOOK, TopicArtifact.Kind.INTEREST_DETAIL_ZIP]).select_related("job").exclude(job__status=TopicJob.Status.EXPIRED),
        id=artifact_id,
        job_id=job_id,
        verified=True,
        expires_at__gt=timezone.now(),
    )
    path = safe_job_path(job_id, artifact.relative_path)
    if not path.is_file():
        return _error("artifact_missing", "结果文件已不存在。", 410)
    _audit(artifact.job, "artifact_downloaded", request.user, {"artifactId": str(artifact.id), "kind": artifact.kind})
    return FileResponse(path.open("rb"), as_attachment=True, filename=artifact.download_name)
