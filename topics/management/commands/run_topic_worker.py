from django.core.management.base import BaseCommand

from topics.worker import run_worker


class Command(BaseCommand):
    help = "Run the single Teeni topic-analysis worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=5.0)

    def handle(self, *args, **options):
        run_worker(once=options["once"], poll_seconds=max(0.1, options["poll_seconds"]))
