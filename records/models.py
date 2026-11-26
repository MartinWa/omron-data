from dataclasses import dataclass

from django.conf import settings
from django.db import models
from django.utils import timezone


@dataclass(frozen=True)
class Metric:
    code: str
    unit: str


METRICS = {
    metric.code: metric
    for metric in (
        Metric("blood_pressure", ""),
        Metric("systolic_bp", "mmHg"),
        Metric("diastolic_bp", "mmHg"),
        Metric("pulse", "/min"),
        Metric("body_weight", "kg"),
        Metric("bmi", ""),
        Metric("body_fat_pct", "%"),
        Metric("skeletal_muscle_pct", "%"),
        Metric("body_water_pct", "%"),
        Metric("visceral_fat", ""),
        Metric("resting_metabolism", "kcal"),
        Metric("impedance", "ohm"),
        Metric("fat_mass", "kg"),
        Metric("average_weight_reported", "kg"),
        Metric("average_fat_mass", "kg"),
        Metric("body_temperature", "°C"),
    )
}

COMPONENTS = ("systolic_bp", "diastolic_bp", "pulse")
PANEL = "blood_pressure"


class ImportRun(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="imports"
    )
    file_name = models.CharField(max_length=255)
    content_sha256 = models.CharField(max_length=64, db_index=True)
    readings = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    conflicts = models.PositiveIntegerField(default=0)
    overwritten = models.PositiveIntegerField(default=0)
    panels = models.PositiveIntegerField(default=0)
    first_at = models.DateTimeField(null=True, blank=True)
    last_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self) -> str:
        return f"{self.file_name} ({self.imported_at:%Y-%m-%d %H:%M})"


class Measurement(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="measurements"
    )
    metric = models.CharField(max_length=32)
    value = models.DecimalField(max_digits=9, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=16, blank=True)
    at = models.DateTimeField()
    note = models.CharField(max_length=100, blank=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="components"
    )
    import_run = models.ForeignKey(
        "records.ImportRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="measurements",
    )

    class Meta:
        ordering = ["-at"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "metric", "at"], name="uniq_measurement_owner_metric_at"
            )
        ]
        indexes = [
            models.Index(fields=["owner", "-at"]),
            models.Index(fields=["owner", "metric", "-at"]),
        ]

    def __str__(self) -> str:
        return f"{self.metric} {self.value if self.value is not None else ''}".strip()


class ApiToken(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens"
    )
    name = models.CharField(max_length=64)
    prefix = models.CharField(max_length=12, db_index=True)
    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="uniq_token_owner_name")
        ]

    @property
    def status(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return "expired"
        return "active"

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix})"
