import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update the initial Teeni administrator without resetting its password."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="Reset an existing administrator to TEENI_ADMIN_PASSWORD.",
        )

    def handle(self, *args, **options):
        email = os.environ.get("TEENI_ADMIN_EMAIL", "").strip().lower()
        password = os.environ.get("TEENI_ADMIN_PASSWORD", "")
        if not email:
            raise CommandError("TEENI_ADMIN_EMAIL is required")

        user_model = get_user_model()
        user = user_model.objects.filter(username=email).first()
        created = user is None
        if created:
            if not password:
                raise CommandError("TEENI_ADMIN_PASSWORD is required for a new administrator")
            user = user_model(username=email, email=email)
        elif options["reset_password"] and not password:
            raise CommandError("TEENI_ADMIN_PASSWORD is required with --reset-password")

        user.email = email
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        if created or options["reset_password"]:
            user.set_password(password)
        user.save()
        action = "created" if created else "updated"
        self.stdout.write(self.style.SUCCESS(f"Administrator {action}: {email}"))
