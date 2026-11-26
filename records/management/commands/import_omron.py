from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from parsing.report import UnknownReportError
from records.importer import COUNTS, INVALID, REPORTED, Rejected, import_report

REPLAY_OVERWRITE = (
    "--replay and --overwrite cannot be combined: replay only fills gaps, "
    "overwrite replaces stored readings"
)

LABELS = {
    "created": "created",
    "skipped": "skipped",
    "conflicts": "conflicts",
    "overwritten": "overwritten",
    "panels": "new panels",
}


def _files(givens: list[Path]) -> list[Path]:
    found: list[Path] = []
    for given in givens:
        if given.is_dir():
            found.extend(sorted(given.glob("*.csv")))
            continue
        found.extend(sorted(path for path in given.parent.glob(given.name) if path.is_file()))
    return found


def stamp(at: datetime) -> str:
    return timezone.localtime(at).strftime("%Y-%m-%d %H:%M")


def _value(value: Decimal | None) -> str:
    return "" if value is None else format(value.normalize(), "f")


def _count(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _rejected_line(item: Rejected) -> str:
    if item.kind == INVALID:
        return (
            f"  {item.at:%Y-%m-%d %H:%M} does not exist in {settings.OMRON_TIME_ZONE}, "
            "so it was not imported; check OMRON_TIME_ZONE if the zone is wrong"
        )
    return (
        f"  {stamp(item.at)} {item.metric}: stored {_value(item.stored)}, "
        f"file {_value(item.incoming)}"
    )


def _line(report: dict[str, object]) -> str:
    counts = ", ".join(f"{report[field]} {LABELS[field]}" for field in COUNTS)
    prefix = ""
    if report["already_imported"] is not None:
        prefix = (
            f"already imported at {stamp(report['already_imported'])}, "
            "pass --replay to import it again without overwriting, "
        )
    return f"{prefix}{report['readings']} readings, {counts}"


class Command(BaseCommand):
    help = "Import OMRON connect CSV reports for one owner."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", type=Path)
        parser.add_argument("--owner", required=True, help="username the readings belong to")
        parser.add_argument("--create-user", action="store_true", help="create the owner if absent")
        flags = parser.add_mutually_exclusive_group()
        flags.add_argument(
            "--overwrite",
            action="store_true",
            help="replace stored readings that differ from the file",
        )
        flags.add_argument(
            "--replay",
            action="store_true",
            help="import a file whose checksum is known without overwriting stored values",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="report the counts without writing anything"
        )

    def handle(self, *args, **options):
        if options["replay"] and options["overwrite"]:
            raise CommandError(REPLAY_OVERWRITE)
        files = _files(options["paths"])
        if not files:
            raise CommandError("no files matched")

        if not options["dry_run"]:
            self._run(files, options)
            return
        for text in self._preview(files, options):
            self.stdout.write(text)

    def _preview(self, files: list[Path], options) -> list[str]:
        with transaction.atomic():
            texts = self._run(files, options)
            transaction.set_rollback(True)
        return texts

    def _run(self, files: list[Path], options) -> list[str]:
        total = dict.fromkeys(REPORTED, 0)
        readings_total = 0
        texts: list[str] = []
        owner = self._owner(options)
        imported = 0
        known = 0
        for position, path in enumerate(files):
            try:
                report = import_report(
                    owner, path, overwrite=options["overwrite"], replay=options["replay"]
                )
            except UnknownReportError as error:
                raise CommandError(
                    self._stop(
                        path,
                        error,
                        position,
                        len(files),
                        imported,
                        known,
                        options["dry_run"],
                    )
                ) from error
            except Exception as error:
                self._stop(path, error, position, len(files), imported, known, options["dry_run"])
                raise
            if report["run"] is None:
                known += int(report["already_imported"] is not None)
            else:
                imported += 1
            readings_total += report["readings"]
            for field in REPORTED:
                total[field] += report[field]
            lines = [f"{path.name}: {_line(report)}"]
            lines.extend(_rejected_line(item) for item in report["rejected"])
            texts.extend(lines)
            self._write(lines, options["dry_run"])

        texts.append(
            f"total: {len(files)} files, {readings_total} readings, "
            f"{total['created']} created, {total['skipped']} skipped, "
            f"{total['conflicts']} conflicts, {total['overwritten']} overwritten, "
            f"{total['panels']} new panels, {total['invalid']} invalid"
        )
        self._write(texts[-1:], options["dry_run"])
        return texts

    def _write(self, lines: list[str], dry_run: bool) -> None:
        if dry_run:
            return
        for line in lines:
            self.stdout.write(line)

    def _stop(
        self,
        path: Path,
        error: Exception,
        position: int,
        count: int,
        imported: int,
        known: int,
        dry_run: bool,
    ) -> str:
        remaining = count - position - 1
        self.stdout.write(f"stopped at {path.name}: {error}")
        if dry_run:
            self.stdout.write(
                f"{_count(position, 'file')} previewed before the error; "
                f"{_count(remaining, 'file')} not attempted; nothing was written"
            )
            return f"{path.name}: {error}; nothing was written"
        self.stdout.write(
            f"{_count(imported, 'file')} imported before the error; "
            f"{_count(known, 'file')} already imported; "
            f"{_count(remaining, 'file')} not attempted"
        )
        if not imported:
            return f"{path.name}: {error}"
        kept = _count(imported, "file")
        return f"{path.name}: {error}; {kept} imported before the error was kept"

    def _owner(self, options):
        user_model = get_user_model()
        if options["create_user"]:
            owner, _ = user_model.objects.get_or_create(username=options["owner"])
            return owner
        owner = user_model.objects.filter(username=options["owner"]).first()
        if owner is None:
            raise CommandError(f"unknown owner: {options['owner']}")
        return owner
