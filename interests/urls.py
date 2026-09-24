from django.urls import path

from . import api
from .preferences import preferences


app_name = "interests"

urlpatterns = [
    path("api/interests/preferences/", preferences, name="preferences"),
    path("api/interests/", api.list_interests, name="list"),
    path("api/interests/segments/", api.segment_summary, name="segments"),
    path("api/interests/segments/groups/", api.segment_groups, name="segment-groups"),
    path("api/interests/candidates/", api.list_candidates, name="candidates"),
    path("api/interests/entities/", api.list_entities, name="entities"),
    path("api/interests/candidates/<uuid:candidate_id>/review/", api.review, name="review"),
    path("api/interests/entities/<str:registry_id>/aliases/", api.add_alias, name="add-alias"),
]
