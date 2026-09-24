from django.urls import path

from . import api


app_name = "topics"

urlpatterns = [
    path("api/interest-jobs", api.interest_jobs, name="create-job"),
    path("api/interest-jobs/", api.interest_jobs, name="list-jobs"),
    path("api/interest-jobs/<uuid:job_id>", api.job_detail, name="job-detail"),
    path("api/interest-jobs/<uuid:job_id>/upload", api.upload_chunk, name="upload-chunk"),
    path("api/interest-jobs/<uuid:job_id>/complete-upload", api.complete_upload, name="complete-upload"),
    path("api/interest-jobs/<uuid:job_id>/events", api.job_events, name="job-events"),
    path("api/interest-jobs/<uuid:job_id>/retry", api.retry_job, name="retry-job"),
    path(
        "api/interest-jobs/<uuid:job_id>/artifacts/<uuid:artifact_id>",
        api.download_artifact,
        name="download-artifact",
    ),
    path("api/topic-jobs", api.interest_jobs, name="legacy-create-job"),
    path("api/topic-jobs/", api.interest_jobs, name="legacy-list-jobs"),
    path("api/topic-jobs/<uuid:job_id>", api.job_detail, name="legacy-job-detail"),
    path("api/topic-jobs/<uuid:job_id>/upload", api.upload_chunk, name="legacy-upload-chunk"),
    path("api/topic-jobs/<uuid:job_id>/complete-upload", api.complete_upload, name="legacy-complete-upload"),
    path("api/topic-jobs/<uuid:job_id>/events", api.job_events, name="legacy-job-events"),
    path("api/topic-jobs/<uuid:job_id>/retry", api.retry_job, name="legacy-retry-job"),
    path(
        "api/topic-jobs/<uuid:job_id>/artifacts/<uuid:artifact_id>",
        api.download_artifact,
        name="legacy-download-artifact",
    ),
]
