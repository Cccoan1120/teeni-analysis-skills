import json
import os
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings

from .models import TopicJob


NOTIFIABLE = {
    TopicJob.Status.COMPLETED: "发布完成",
    TopicJob.Status.FAILED: "失败",
    TopicJob.Status.WAITING_MODEL: "等待模型",
}


class NotificationError(RuntimeError):
    pass


def build_notification_payload(job: TopicJob) -> dict:
    if job.status not in NOTIFIABLE:
        raise NotificationError("job status is not notifiable")
    base_url = settings.TEENI_PUBLIC_BASE_URL.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise NotificationError("public dashboard URL is invalid")
    link = f"{base_url}/?view=interest-jobs&version={job.product_version}"
    text = f"Teeni {job.product_version} 兴趣任务\n日期：{job.data_date.isoformat()}\n状态：{NOTIFIABLE[job.status]}\n看板：{link}"
    return {"msg_type": "text", "content": {"text": text}}


def _read_webhook() -> str:
    path_value = settings.TEENI_FEISHU_WEBHOOK_FILE
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.is_file():
        raise NotificationError("Feishu webhook credential is missing")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise NotificationError("Feishu webhook credential must use mode 600")
    value = path.read_text(encoding="utf-8").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or "\n" in value or "\r" in value:
        raise NotificationError("Feishu webhook credential is invalid")
    return value


def notify_job(job: TopicJob, *, opener=urlopen) -> bool:
    webhook = _read_webhook()
    if not webhook:
        return False
    body = json.dumps(build_notification_payload(job), ensure_ascii=False).encode("utf-8")
    request = Request(webhook, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise NotificationError("Feishu notification returned a non-success status")
    except OSError as exc:
        raise NotificationError("Feishu notification could not be delivered") from exc
    return True
