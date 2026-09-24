from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="DailySnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("data_date", models.DateField(unique=True)),
                ("scene_id", models.CharField(max_length=16)),
                ("core_version", models.CharField(max_length=32)),
                ("rules_version", models.CharField(max_length=32)),
                ("contract_version", models.CharField(max_length=64)),
                ("source_sha256", models.CharField(max_length=64)),
                ("workbook_sha256", models.CharField(max_length=64)),
                ("payload", models.JSONField()),
                ("published_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["data_date"]},
        )
    ]
