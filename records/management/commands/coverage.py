from datetime import datetime, timedelta
from itertools import pairwise

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from records.models import Measurement

GAP = timedelta(days=7)
HEADER = f"{'metric':<22} {'count':>6}  {'first':<16}  {'last':<16} {'gaps':>5}"


def _stamp(at: datetime) -> str:
    return timezone.localtime(at).strftime("%Y-%m-%d %H:%M")


def _span(delta: timedelta) -> str:
    return f"{delta.days}d {delta.seconds // 3600}h"


class Command(BaseCommand):
    help = "Report per-metric coverage and the gaps longer than seven days for one owner."

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True, help="username to report on")

    def handle(self, *args, **options):
        owner = get_user_model().objects.filter(username=options["owner"]).first()
        if owner is None:
            raise CommandError(f"unknown owner: {options['owner']}")

        metrics = (
            Measurement.objects.filter(owner=owner)
            .order_by("metric")
            .values_list("metric", flat=True)
            .distinct()
        )
        self.stdout.write(HEADER)
        for metric in metrics:
            moments = list(
                Measurement.objects.filter(owner=owner, metric=metric)
                .order_by("at")
                .values_list("at", flat=True)
            )
            gaps = [(before, after) for before, after in pairwise(moments) if after - before > GAP]
            self.stdout.write(
                f"{metric:<22} {len(moments):>6}  {_stamp(moments[0]):<16}  "
                f"{_stamp(moments[-1]):<16} {len(gaps):>5}"
            )
            for before, after in gaps:
                self.stdout.write(
                    f"  gap {_stamp(before)} -> {_stamp(after)} ({_span(after - before)})"
                )
