from django.db import migrations

RENAMES = {
    "fat_free_mass": "fat_mass",
    "average_fat_free_mass": "average_fat_mass",
    "average_weight": "average_weight_change",
}


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
        ("records", "0004_apitoken_expiry"),
    ]

    operations = [
        migrations.RunPython(rename_forward, rename_back),
    ]
