from django.contrib import admin

from .models import (
    InterestAlias,
    InterestBackfillRequest,
    InterestCandidate,
    InterestEntity,
    InterestRegistrySnapshot,
    InterestReviewEvent,
    InterestSnapshot,
)


admin.site.register(InterestEntity)
admin.site.register(InterestAlias)
admin.site.register(InterestCandidate)
admin.site.register(InterestSnapshot)
admin.site.register(InterestRegistrySnapshot)
admin.site.register(InterestBackfillRequest)
admin.site.register(InterestReviewEvent)
