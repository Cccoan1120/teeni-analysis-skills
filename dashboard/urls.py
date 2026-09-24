from django.urls import path

from . import views
from .research_api import research_data


app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/dashboard/", views.dashboard_data, name="data"),
    path("api/research/", research_data, name="research-data"),
    path("api/publish/", views.publish_snapshot, name="publish"),
    path("api/insights/", views.insights, name="insights"),
    path("api/insights/latest/", views.latest_insight, name="latest-insight"),
    path("api/insights/<uuid:insight_id>/", views.insight_detail, name="insight-detail"),
    path("api/insights/<uuid:insight_id>/publish/", views.insight_publish, name="insight-publish"),
    path("api/insights/<uuid:insight_id>/withdraw/", views.insight_withdraw, name="insight-withdraw"),
    path("api/insights/<uuid:insight_id>/history/", views.insight_history, name="insight-history"),
    path("health/", views.health, name="health"),
]
