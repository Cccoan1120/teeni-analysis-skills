from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("interests", "0003_interestcandidate_model_recommended"),
        ("topics", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="InterestSegmentSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("data_date", models.DateField()),
                ("dimension_set", models.CharField(max_length=32)),
                ("schema_version", models.CharField(max_length=64)),
                ("source_sha256", models.CharField(max_length=64)),
                ("registry_sha256", models.CharField(max_length=64)),
                ("province_map_version", models.CharField(max_length=64)),
                ("province_map_sha256", models.CharField(max_length=64)),
                ("payload", models.JSONField()),
                ("published_at", models.DateTimeField(auto_now=True)),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="interest_segment_snapshots",
                        to="topics.topicjob",
                    ),
                ),
            ],
            options={"ordering": ["data_date", "dimension_set"]},
        ),
        migrations.AddConstraint(
            model_name="interestsegmentsnapshot",
            constraint=models.UniqueConstraint(
                fields=("data_date", "dimension_set"),
                name="unique_interest_segment_per_date_dimension",
            ),
        ),
    ]
