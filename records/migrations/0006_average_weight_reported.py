from django.db import migrations

RENAMES = {"average_weight_change": "average_weight_reported"}


def rename_forward(apps, schema_editor):
    Measurement = apps.get_model("records", "Measurement")
    for old, new in RENAMES.items():
        Measurement.objects.filter(metric=old).update(metric=new)


def rename_back(apps, schema_editor):
    Measurement = apps.get_model("records", "Measurement")
    for old, new in RENAMES.items():
        Measurement.objects.filter(metric=new).update(metric=old)


class Migration(migrations.Migration):
    dependencies = [
        ("records", "0005_rename_body_composition_metrics"),
    ]

    operations = [
        migrations.RunPython(rename_forward, rename_back),
    ]
