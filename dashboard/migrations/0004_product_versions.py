from django.db import migrations, models


def backfill_m1(apps, schema_editor):
    DailySnapshot = apps.get_model("dashboard", "DailySnapshot")
    ManagementInsight = apps.get_model("dashboard", "ManagementInsight")
    DailySnapshot.objects.filter(product_version__isnull=True).update(product_version="M1")
    ManagementInsight.objects.filter(product_version__isnull=True).update(product_version="M1")


def clear_product_versions(apps, schema_editor):
    DailySnapshot = apps.get_model("dashboard", "DailySnapshot")
    ManagementInsight = apps.get_model("dashboard", "ManagementInsight")
    DailySnapshot.objects.update(product_version=None)
    ManagementInsight.objects.update(product_version=None)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0003_managementinsight_managementinsightrevision"),
    ]

    operations = [
        migrations.AddField(
            model_name="dailysnapshot",
            name="product_version",
            field=models.CharField(blank=True, max_length=2, null=True),
        ),
        migrations.AddField(
            model_name="managementinsight",
            name="product_version",
            field=models.CharField(blank=True, max_length=2, null=True),
        ),
        migrations.RunPython(backfill_m1, clear_product_versions),
        migrations.AlterField(
            model_name="dailysnapshot",
            name="product_version",
            field=models.CharField(choices=[("M1", "M1"), ("M2", "M2")], default="M1", max_length=2),
        ),
        migrations.AlterField(
            model_name="managementinsight",
            name="product_version",
            field=models.CharField(choices=[("M1", "M1"), ("M2", "M2")], default="M1", max_length=2),
        ),
        migrations.AlterField(
            model_name="dailysnapshot",
            name="data_date",
            field=models.DateField(),
        ),
        migrations.AlterModelOptions(
            name="dailysnapshot",
            options={"ordering": ["product_version", "data_date"]},
        ),
        migrations.AddConstraint(
            model_name="dailysnapshot",
            constraint=models.UniqueConstraint(
                fields=("data_date", "product_version"),
                name="unique_daily_snapshot_product_date",
            ),
        ),
        migrations.AddConstraint(
            model_name="dailysnapshot",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("product_version", "M1"), ("scene_id", "488"))
                    | models.Q(("product_version", "M2"), ("scene_id", "904"))
                ),
                name="daily_snapshot_product_scene_matches",
            ),
        ),
    ]
