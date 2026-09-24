from functools import wraps

from django.http import JsonResponse

from dashboard.product_versions import DEFAULT_PRODUCT_VERSION, PRODUCT_VERSION_SCENES, is_product_version


def payload_product_version(payload):
    version = payload.get("productVersion", DEFAULT_PRODUCT_VERSION)
    scene = payload.get("sceneId", PRODUCT_VERSION_SCENES[DEFAULT_PRODUCT_VERSION])
    if not is_product_version(version) or str(scene) != PRODUCT_VERSION_SCENES[version]:
        raise ValueError("interest product version and scene mismatch")
    return version


def versioned_request(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        version = request.GET.get("productVersion", DEFAULT_PRODUCT_VERSION)
        if not is_product_version(version):
            return JsonResponse({"error": "invalid_product_version", "detail": "productVersion must be M1 or M2"}, status=400)
        request.product_version = version
        return view(request, *args, **kwargs)
    return wrapped
