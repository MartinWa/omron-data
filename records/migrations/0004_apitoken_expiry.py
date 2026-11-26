from django.db import migrations, models
from django.utils import timezone


def mark_revoked_tokens(apps, schema_editor):
    ApiToken = apps.get_model("records", "ApiToken")
    ApiToken.objects.filter(revoked=True).update(revoked_at=timezone.now())


def restore_revoked_tokens(apps, schema_editor):
    ApiToken = apps.get_model("records", "ApiToken")
    ApiToken.objects.exclude(revoked_at__isnull=True, expires_at__isnull=True).update(revoked=True)


class Migration(migrations.Migration):
    dependencies = [
        ("records", "0003_importrun_measurement_import_run"),
    ]

    operations = [
        migrations.AddField(
            model_name="apitoken",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="apitoken",
            name="revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="apitoken",
            name="prefix",
            field=models.CharField(db_index=True, max_length=12),
        ),
        migrations.RunPython(mark_revoked_tokens, restore_revoked_tokens),
        migrations.RemoveField(
            model_name="apitoken",
            name="revoked",
        ),
    ]
