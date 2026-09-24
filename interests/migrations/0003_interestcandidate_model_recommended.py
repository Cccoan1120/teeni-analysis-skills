from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("interests", "0002_entity_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="interestcandidate",
            name="model_recommended",
            field=models.BooleanField(default=False),
        ),
    ]
