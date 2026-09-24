from __future__ import annotations

from interest_engine.segments import DIMENSION_SETS, dimension_set_id

from dashboard.product_versions import PRODUCT_VERSION_SCENES
from .product_versions import payload_product_version
from .models import InterestSegmentSnapshot


def stored_segment_payload(sidecar: dict, set_id: str) -> dict:
    segment_set = sidecar["dimensionSets"][set_id]
    return {
        "schemaVersion": sidecar["schemaVersion"],
        "dataDate": sidecar["dataDate"],
        "productVersion": payload_product_version(sidecar),
        "sceneId": PRODUCT_VERSION_SCENES[payload_product_version(sidecar)],
        "dimensionSet": set_id,
        "dimensions": segment_set["dimensions"],
        "sampleMinimumUsers": sidecar["sampleMinimumUsers"],
        "domains": sidecar["domains"],
        "overallEligibleUsers": segment_set["overallEligibleUsers"],
        "overallIps": segment_set["overallIps"],
        "groups": segment_set["groups"],
    }


def replace_segment_snapshots(job, sidecar: dict) -> None:
    if payload_product_version(sidecar) != job.product_version:
        raise ValueError("interest segment product version mismatch")
    InterestSegmentSnapshot.objects.filter(product_version=job.product_version, data_date=job.data_date).delete()
    InterestSegmentSnapshot.objects.bulk_create([
        InterestSegmentSnapshot(
            product_version=job.product_version,
            data_date=job.data_date,
            job=job,
            dimension_set=set_id,
            schema_version=sidecar["schemaVersion"],
            source_sha256=sidecar["sourceSha256"],
            registry_sha256=sidecar["registrySha256"],
            province_map_version=sidecar["provinceMapVersion"],
            province_map_sha256=sidecar["provinceMapSha256"],
            payload=stored_segment_payload(sidecar, set_id),
        )
        for set_id in (dimension_set_id(dimensions) for dimensions in DIMENSION_SETS)
    ])
