from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from records.models import ImportRun, Measurement


class Command(BaseCommand):
    help = "Delete the measurements created by one import run, then the run itself."

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, required=True, help="import run to delete")
        parser.add_argument("--owner", required=True, help="username the run belongs to")
        parser.add_argument(
            "--dry-run", action="store_true", help="report how many rows would go, delete nothing"
        )

    @transaction.atomic
    def handle(self, *args, **options):
        run = self._run(options)
        run_id = run.pk
        doomed = Measurement.objects.filter(import_run=run)
        rows = doomed.count()

        if options["dry_run"]:
            self.stdout.write(f"run {run_id} ({run.file_name}): would delete {rows} measurements")
            return

        Measurement.objects.filter(parent__in=doomed).exclude(import_run=run).update(parent=None)
        doomed.delete()
        run.delete()
        self.stdout.write(f"run {run_id} ({run.file_name}): deleted {rows} measurements")

    def _run(self, options) -> ImportRun:
        owner = get_user_model().objects.filter(username=options["owner"]).first()
        if owner is None:
            raise CommandError(f"unknown owner: {options['owner']}")
        run = ImportRun.objects.filter(pk=options["id"]).first()
        if run is None:
            raise CommandError(f"unknown import run: {options['id']}")
        if run.owner_id != owner.pk:
            raise CommandError(
                f"import run {options['id']} belongs to {run.owner.username}, not {owner.username}"
            )
        return run
