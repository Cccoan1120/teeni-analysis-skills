from django.utils import timezone

from .models import TopicArtifact, TopicAuditEvent, TopicJob


def serialize_artifact(artifact: TopicArtifact) -> dict:
    return {
        "id": str(artifact.id),
        "kind": artifact.kind,
        "downloadName": artifact.download_name,
        "sha256": artifact.sha256,
        "byteSize": artifact.byte_size,
        "verified": artifact.verified,
        "expiresAt": artifact.expires_at.isoformat(),
        "downloadUrl": f"/api/interest-jobs/{artifact.job_id}/artifacts/{artifact.id}?productVersion={artifact.job.product_version}",
    }


def serialize_event(event: TopicAuditEvent) -> dict:
    return {
        "id": event.id,
        "action": event.action,
        "actor": event.actor.email if event.actor_id and event.actor.email else None,
        "details": event.details,
        "createdAt": event.created_at.isoformat(),
    }


def serialize_job(job: TopicJob, *, include_events: bool = False) -> dict:
    artifacts = [
        serialize_artifact(item)
        for item in job.artifacts.all()
        if item.verified and item.expires_at > timezone.now()
        and item.kind in {TopicArtifact.Kind.INTEREST_WORKBOOK, TopicArtifact.Kind.INTEREST_DETAIL_ZIP}
    ]
    payload = {
        "id": str(job.id),
        "dataDate": job.data_date.isoformat(),
        "productVersion": job.product_version,
        "sceneId": job.scene_id,
        "originalName": job.original_name,
        "pipeline": job.pipeline,
        "pipelineLabel": job.get_pipeline_display(),
        "expectedSize": job.expected_size,
        "receivedSize": job.received_size,
        "inputSha256": job.input_sha256 or None,
        "status": job.status,
        "statusLabel": job.get_status_display(),
        "progress": job.progress,
        "errorCode": job.error_code or None,
        "errorMessage": job.error_message or None,
        "retryCount": job.retry_count,
        "artifacts": artifacts,
        "expiresAt": job.expires_at.isoformat(),
        "createdAt": job.created_at.isoformat(),
        "updatedAt": job.updated_at.isoformat(),
        "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
    }
    if include_events:
        payload["events"] = [serialize_event(item) for item in job.events.select_related("actor").all()]
    return payload
