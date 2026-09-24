import hmac
import json
from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from publisher.privacy import AggregatePrivacyError, assert_aggregate_only
from publisher.snapshot import SnapshotContractError, validate_snapshot_payload

from .forms import EmailRegistrationForm
from .insights import (
    InsightConflictError,
    InsightValidationError,
    create_generated_insight,
    ensure_daily_draft,
    publish_revision,
    save_revision,
    serialize_insight,
    serialize_revision,
    withdraw_insight,
)
from .models import DailySnapshot, ManagementInsight
from .product_versions import (
    DEFAULT_PRODUCT_VERSION,
    PRODUCT_VERSION_SCENES,
    is_product_version,
    product_version_for_scene,
)


def _json_body(request: HttpRequest) -> dict:
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InsightValidationError("请求正文不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise InsightValidationError("请求正文必须是对象。")
    return payload


def _insight_error(code: str, detail: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": code, "detail": detail}, status=status)


def _product_version(value: object) -> str:
    if value in (None, ""):
        return DEFAULT_PRODUCT_VERSION
    if not is_product_version(value):
        raise InsightValidationError("机器版本必须是 M1 或 M2。")
    return str(value)


def _load_baseline(path: Path) -> dict:
    if not path.exists():
        return {"schemaVersion": "teeni-dashboard-baseline/1.0.0", "comparisons": []}
    return json.loads(path.read_text(encoding="utf-8"))


@require_http_methods(["GET", "POST"])
def register(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("dashboard:index")

    next_url = request.POST.get("next", request.GET.get("next", ""))
    form = EmailRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save()
        except IntegrityError:
            form.add_error("email", "该邮箱已注册。")
        else:
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect(reverse("dashboard:index"))
    return render(request, "registration/register.html", {"form": form, "next": next_url})


@login_required
def index(request: HttpRequest):
    return render(request, "dashboard/index.html")


@require_GET
@login_required
def dashboard_data(request: HttpRequest) -> JsonResponse:
    try:
        product_version = _product_version(request.GET.get("productVersion"))
    except InsightValidationError as exc:
        return _insight_error("invalid_product_version", str(exc))
    rows = DailySnapshot.objects.filter(product_version=product_version).order_by("data_date")
    snapshots = [
        {
            **row.payload,
            "publishedAt": row.published_at.isoformat(),
            "productVersion": row.product_version,
            "sceneId": row.scene_id,
        }
        for row in rows
    ]
    versions = []
    for version, scene_id in PRODUCT_VERSION_SCENES.items():
        latest = (
            DailySnapshot.objects.filter(product_version=version)
            .order_by("-data_date")
            .values_list("data_date", flat=True)
            .first()
        )
        versions.append(
            {
                "productVersion": version,
                "sceneId": scene_id,
                "latestDate": latest.isoformat() if latest else None,
            }
        )
    return JsonResponse(
        {
            "schemaVersion": "teeni-dashboard-response/1.2.0",
            "selectedProductVersion": product_version,
            "versions": versions,
            "snapshots": snapshots,
            "baseline": (
                _load_baseline(settings.TEENI_BASELINE_PATH)
                if product_version == "M1"
                else None
            ),
            "latestDate": snapshots[-1]["dataDate"] if snapshots else None,
        },
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
@require_POST
def publish_snapshot(request: HttpRequest) -> JsonResponse:
    configured = settings.TEENI_PUBLISH_TOKEN
    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {configured}" if configured else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
        validate_snapshot_payload(payload)
        assert_aggregate_only(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, SnapshotContractError, AggregatePrivacyError) as exc:
        return JsonResponse({"error": "invalid_snapshot", "detail": str(exc)}, status=400)

    product_version = payload.get("productVersion") or product_version_for_scene(payload["sceneId"])
    with transaction.atomic():
        snapshot, _ = DailySnapshot.objects.update_or_create(
            data_date=payload["dataDate"],
            product_version=product_version,
            defaults={
                "scene_id": payload["sceneId"],
                "core_version": payload["coreVersion"],
                "rules_version": payload["rulesVersion"],
                "contract_version": payload["contractVersion"],
                "source_sha256": payload["sourceSha256"],
                "workbook_sha256": payload["workbookSha256"],
                "payload": payload,
            },
        )
        ensure_daily_draft(snapshot)
    return JsonResponse(
        {
            "ok": True,
            "dataDate": payload["dataDate"],
            "productVersion": product_version,
            "sceneId": payload["sceneId"],
            "workbookSha256": payload["workbookSha256"],
        },
        status=201,
    )


@require_http_methods(["GET", "POST"])
@login_required
def insights(request: HttpRequest) -> JsonResponse:
    try:
        requested_version = _product_version(
            request.GET.get("productVersion") if request.method == "GET" else None
        )
    except InsightValidationError as exc:
        return _insight_error("invalid_product_version", str(exc))
    if request.method == "GET":
        queryset = (
            ManagementInsight.objects.filter(product_version=requested_version)
            .select_related("created_by", "published_by")
            .prefetch_related("revisions__created_by")
        )
        status = request.GET.get("status")
        if status:
            if status not in {item.value for item in ManagementInsight.Status}:
                return _insight_error("invalid_status", "说明状态无效。")
            queryset = queryset.filter(status=status)
        return JsonResponse(
            {"items": [serialize_insight(item) for item in queryset[:100]]},
            json_dumps_params={"ensure_ascii": False},
        )

    try:
        payload = _json_body(request)
        requested_version = _product_version(payload.get("productVersion"))
        start_date = date.fromisoformat(str(payload.get("startDate", "")))
        end_date = date.fromisoformat(str(payload.get("endDate", "")))
        insight = create_generated_insight(
            start_date,
            end_date,
            request.user,
            product_version=requested_version,
        )
    except (TypeError, ValueError, InsightValidationError) as exc:
        return _insight_error("invalid_insight", str(exc))
    return JsonResponse({"insight": serialize_insight(insight, include_history=True)}, status=201, json_dumps_params={"ensure_ascii": False})


@require_http_methods(["GET", "PUT"])
@login_required
def insight_detail(request: HttpRequest, insight_id) -> JsonResponse:
    insight = get_object_or_404(ManagementInsight, id=insight_id)
    if request.method == "GET":
        return JsonResponse({"insight": serialize_insight(insight, include_history=True)}, json_dumps_params={"ensure_ascii": False})
    try:
        insight = save_revision(insight.id, _json_body(request), request.user)
    except InsightConflictError as exc:
        return _insight_error("revision_conflict", str(exc), 409)
    except InsightValidationError as exc:
        return _insight_error("invalid_insight", str(exc))
    return JsonResponse({"insight": serialize_insight(insight, include_history=True)}, json_dumps_params={"ensure_ascii": False})


@require_POST
@login_required
def insight_publish(request: HttpRequest, insight_id) -> JsonResponse:
    try:
        payload = _json_body(request)
        insight = publish_revision(insight_id, int(payload.get("revision")), request.user)
    except ManagementInsight.DoesNotExist:
        return _insight_error("not_found", "说明不存在。", 404)
    except (TypeError, ValueError, InsightValidationError) as exc:
        return _insight_error("invalid_revision", str(exc))
    return JsonResponse({"insight": serialize_insight(insight, include_history=True)}, json_dumps_params={"ensure_ascii": False})


@require_POST
@login_required
def insight_withdraw(request: HttpRequest, insight_id) -> JsonResponse:
    try:
        insight = withdraw_insight(insight_id, request.user)
    except ManagementInsight.DoesNotExist:
        return _insight_error("not_found", "说明不存在。", 404)
    except InsightValidationError as exc:
        return _insight_error("invalid_state", str(exc), 409)
    return JsonResponse({"insight": serialize_insight(insight, include_history=True)}, json_dumps_params={"ensure_ascii": False})


@require_GET
@login_required
def insight_history(request: HttpRequest, insight_id) -> JsonResponse:
    insight = get_object_or_404(ManagementInsight, id=insight_id)
    return JsonResponse(
        {"items": [serialize_revision(item) for item in insight.revisions.select_related("created_by").all()]},
        json_dumps_params={"ensure_ascii": False},
    )


@require_GET
@login_required
def latest_insight(request: HttpRequest) -> JsonResponse:
    try:
        product_version = _product_version(request.GET.get("productVersion"))
    except InsightValidationError as exc:
        return _insight_error("invalid_product_version", str(exc))
    insight = (
        ManagementInsight.objects.filter(
            status=ManagementInsight.Status.PUBLISHED,
            product_version=product_version,
        )
        .order_by("-published_at")
        .first()
    )
    return JsonResponse(
        {"insight": serialize_insight(insight) if insight else None},
        json_dumps_params={"ensure_ascii": False},
    )


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})
