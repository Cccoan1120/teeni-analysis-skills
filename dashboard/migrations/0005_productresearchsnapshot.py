from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0004_product_versions")]
    operations = [migrations.CreateModel(
        name="ProductResearchSnapshot",
        fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("product_version", models.CharField(choices=[("M1", "M1"), ("M2", "M2")], max_length=2)),
                ("data_date", models.DateField()), ("payload", models.JSONField()),
                ("source_sha256", models.CharField(max_length=64)), ("published_at", models.DateTimeField(auto_now=True))],
        options={"constraints": [models.UniqueConstraint(fields=("product_version", "data_date"), name="unique_research_product_date")]},
    )]
