from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="BulkInsertJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("RUNNING", "Running"), ("SUCCESS", "Success"), ("ERROR", "Error")], default="PENDING", max_length=16)),
                ("source_filename", models.CharField(blank=True, default="", max_length=255)),
                ("source_path", models.TextField(blank=True, default="")),
                ("total_rows", models.IntegerField(blank=True, null=True)),
                ("success_rows", models.IntegerField(default=0)),
                ("error_rows", models.IntegerField(default=0)),
                ("error_message", models.TextField(blank=True, default="")),
            ],
        ),
    ]

