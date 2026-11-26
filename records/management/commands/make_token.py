from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from records.tokens import issue


class Command(BaseCommand):
    help = "Issue an API token for one owner. The secret is shown once."

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True)
        parser.add_argument("--name", required=True)

    def handle(self, *args, **options):
        owner = get_user_model().objects.filter(username=options["owner"]).first()
        if owner is None:
            raise CommandError(f"unknown owner: {options['owner']}")
        key = issue(owner, options["name"])
        self.stdout.write(f"{options['name']}: {key}")
