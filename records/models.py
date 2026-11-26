from dataclasses import dataclass

from django.conf import settings
from django.db import models


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
        Metric("visceral_fat", ""),
        Metric("resting_metabolism", "kcal"),
        Metric("impedance", "ohm"),
        Metric("fat_free_mass", "kg"),
    )
}

COMPONENTS = ("systolic_bp", "diastolic_bp", "pulse")
PANEL = "blood_pressure"


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
    prefix = models.CharField(max_length=12)
    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="uniq_token_owner_name")
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix})"
