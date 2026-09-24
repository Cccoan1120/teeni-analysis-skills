from datetime import date

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from interests.models import InterestSnapshot
from .models import DailySnapshot, ProductResearchSnapshot


@require_GET
@login_required
def research_data(request):
    version = request.GET.get("productVersion", "M1")
    if version not in ("M1", "M2"):
        return JsonResponse({"error": "invalid_product_version"}, status=400)
    requested = request.GET.get("date")
    try:
        target = date.fromisoformat(requested) if requested else DailySnapshot.objects.filter(product_version=version).order_by("-data_date").values_list("data_date", flat=True).first()
    except ValueError:
        return JsonResponse({"error": "invalid_date"}, status=400)
    snapshot = ProductResearchSnapshot.objects.filter(product_version=version, data_date=target).first() if target else None
    products = []
    for product in ("M1", "M2"):
        base = DailySnapshot.objects.filter(product_version=product, data_date=target).first() if target else None
        interest = InterestSnapshot.objects.filter(product_version=product, data_date=target).first() if target else None
        base_latest = DailySnapshot.objects.filter(product_version=product).order_by("-data_date").values_list("data_date", flat=True).first()
        interest_latest = InterestSnapshot.objects.filter(product_version=product).order_by("-data_date").values_list("data_date", flat=True).first()
        detail_hash = base.payload.get("baseDetailSha256") if base else None
        aligned = detail_hash == interest.source_sha256 if detail_hash and interest else None
        products.append({"productVersion": product, "base": {"status": "available" if base else "missing", "latestDate": str(base_latest) if base_latest else None},
                         "interest": {"status": "available" if interest else "missing", "latestDate": str(interest_latest) if interest_latest else None},
                         "alignment": "matched" if aligned else "mismatch" if aligned is False else "not_recorded"})
    stale = False
    if snapshot:
        for source in snapshot.payload.get("sources", []):
            base = DailySnapshot.objects.filter(product_version=version, data_date=source["dataDate"]).first()
            if not base or base.payload.get("baseDetailSha256") != source["detailSha256"]:
                stale = True
    evaluation_status = snapshot.payload.get('evaluationProvenance', {}).get('status', 'current_definition') if snapshot else 'unavailable'
    response = JsonResponse({"productVersion": version, "dataDate": str(target) if target else None, "pipeline": products,
                             "research": snapshot.payload if snapshot else None, "researchStale": stale,
                             "researchEvaluationStatus": evaluation_status})
    response["Cache-Control"] = "no-store"
    return response
