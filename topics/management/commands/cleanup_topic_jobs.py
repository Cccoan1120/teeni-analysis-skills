from django.core.management.base import BaseCommand

from topics.cleanup import cleanup_expired_interest_batches, cleanup_expired_interest_cache, cleanup_expired_jobs


class Command(BaseCommand):
    help = "Delete expired topic-job files while retaining aggregate snapshots."

    def handle(self, *args, **options):
        job_count = cleanup_expired_jobs()
        batch_count = cleanup_expired_interest_batches()
        cache_count = cleanup_expired_interest_cache()
        self.stdout.write(f"Expired job files removed: {job_count}; interest batches removed: {batch_count}; daily caches removed: {cache_count}")
