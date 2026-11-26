from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from records.models import ApiToken
from records.tokens import issue


class Command(BaseCommand):
    help = "Issue an API token for one owner. The secret is shown once."

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument(
            "--expires-days",
            type=int,
            default=None,
            help="Refuse this token after N days. Omit for a token that never expires.",
        )

    def handle(self, *args, **options):
        owner = get_user_model().objects.filter(username=options["owner"]).first()
        if owner is None:
            raise CommandError(f"unknown owner: {options['owner']}")
        days = options["expires_days"]
        if days is not None and not 1 <= days <= 3650:
            raise CommandError("--expires-days must be between 1 and 3650")
        if ApiToken.objects.filter(owner=owner, name=options["name"]).exists():
            raise CommandError(
                f"{options['name']} already exists for {owner.username}; "
                "revoke and delete it first, or choose another name."
            )
        expires_at = timezone.now() + timedelta(days=days) if days else None
        key = issue(owner, options["name"], expires_at=expires_at)
        self.stdout.write(f"{options['name']}: {key}")
        if expires_at is not None:
            self.stdout.write(f"expires {expires_at:%Y-%m-%d}")
