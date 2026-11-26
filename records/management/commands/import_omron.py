from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from parsing.report import parse_file
from records.importer import import_readings

FIELDS = ("created", "skipped", "conflicts", "overwritten", "panels")


def _files(givens: list[Path]) -> list[Path]:
    found: list[Path] = []
    for given in givens:
        if given.is_dir():
            found.extend(sorted(given.glob("*.csv")))
            continue
        found.extend(sorted(path for path in given.parent.glob(given.name) if path.is_file()))
    return found


class Command(BaseCommand):
    help = "Import OMRON connect CSV reports for one owner."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", type=Path)
        parser.add_argument("--owner", required=True, help="username the readings belong to")
        parser.add_argument("--create-user", action="store_true", help="create the owner if absent")
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="replace stored readings that differ from the file",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="report the counts without writing anything"
        )

    def handle(self, *args, **options):
        files = _files(options["paths"])
        if not files:
            raise CommandError("no files matched")

        total = dict.fromkeys(FIELDS, 0)
        readings_total = 0
        with transaction.atomic():
            owner = self._owner(options)
            for path in files:
                readings = parse_file(path)
                counts = import_readings(owner, readings, overwrite=options["overwrite"])
                readings_total += len(readings)
                for field in FIELDS:
                    total[field] += counts[field]
                self.stdout.write(
                    f"{path.name}: {len(readings)} readings, "
                    f"{counts['created']} created, {counts['skipped']} skipped, "
                    f"{counts['conflicts']} conflicts, {counts['overwritten']} overwritten, "
                    f"{counts['panels']} new panels"
                )
            if options["dry_run"]:
                transaction.set_rollback(True)

        self.stdout.write(
            f"total: {len(files)} files, {readings_total} readings, "
            f"{total['created']} created, {total['skipped']} skipped, "
            f"{total['conflicts']} conflicts, {total['overwritten']} overwritten, "
            f"{total['panels']} new panels"
        )

    def _owner(self, options):
        user_model = get_user_model()
        if options["create_user"]:
            owner, _ = user_model.objects.get_or_create(username=options["owner"])
            return owner
        owner = user_model.objects.filter(username=options["owner"]).first()
        if owner is None:
            raise CommandError(f"unknown owner: {options['owner']}")
        return owner
