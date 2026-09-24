from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("interests", "0004_interestsegmentsnapshot")]
    operations = [
        migrations.CreateModel(
            name="InterestDailyContribution",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("data_date", models.DateField(unique=True)),
                ("source_sha256", models.CharField(max_length=64)),
                ("registry_sha256", models.CharField(max_length=64)),
                ("identity_sha256", models.CharField(max_length=64)),
                ("detail_sha256", models.CharField(max_length=64)),
                ("payload", models.BinaryField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["data_date"]},
        ),
    ]
