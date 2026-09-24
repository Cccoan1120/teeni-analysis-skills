from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("topics", "0005_alter_topicjob_pipeline")]

    operations = [
        migrations.AlterField(
            model_name="topicjob",
            name="pipeline",
            field=models.CharField(
                choices=[("legacy_topic", "旧话题分析"), ("shadow", "兴趣与旧话题影子双跑"), ("interest_v1", "IP兴趣分析 v1")],
                default="interest_v1",
                max_length=24,
            ),
        ),
    ]
