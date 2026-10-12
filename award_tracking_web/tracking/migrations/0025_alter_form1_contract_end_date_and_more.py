from datetime import datetime, time, timezone as dt_timezone

from django.db import migrations, models


DATE_FIELD_MAPPINGS = (
    ("contract_start_date", "temporary_contract_start_date"),
    ("contract_end_date", "temporary_contract_end_date"),
    ("internal_gl_start_date", "temporary_internal_gl_start_date"),
    ("internal_gl_end_date", "temporary_internal_gl_end_date"),
)


def copy_datetime_values_to_date_fields(apps, schema_editor):
    """
    Preserve the calendar-date portion of the existing Form1 DateTimeField
    values without converting them to local time.

    Example:
        2025-03-01 00:00:00+00:00 -> 2025-03-01
    """
    Form1 = apps.get_model("tracking", "Form1")
    database_alias = schema_editor.connection.alias

    for grant in Form1.objects.using(database_alias).all().iterator():
        changed_fields = []

        for source_field, target_field in DATE_FIELD_MAPPINGS:
            value = getattr(grant, source_field)

            if value is None:
                continue

            if isinstance(value, datetime):
                value = value.date()

            setattr(grant, target_field, value)
            changed_fields.append(target_field)

        if changed_fields:
            grant.save(
                using=database_alias,
                update_fields=changed_fields,
            )


def restore_date_values_to_datetime_fields(apps, schema_editor):
    """
    Reverse the migration by restoring each business date as midnight UTC.
    """
    Form1 = apps.get_model("tracking", "Form1")
    database_alias = schema_editor.connection.alias

    for grant in Form1.objects.using(database_alias).all().iterator():
        changed_fields = []

        for datetime_field, date_field in DATE_FIELD_MAPPINGS:
            value = getattr(grant, date_field)

            if value is None:
                continue

            restored_value = datetime.combine(
                value,
                time.min,
            ).replace(tzinfo=dt_timezone.utc)

            setattr(
                grant,
                datetime_field,
                restored_value,
            )
            changed_fields.append(datetime_field)

        if changed_fields:
            grant.save(
                using=database_alias,
                update_fields=changed_fields,
            )


class Migration(migrations.Migration):

    dependencies = [
        (
            "tracking",
            "0024_changerequest_unique_new_grant_request_per_grant_id",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="form1",
            name="temporary_contract_start_date",
            field=models.DateField(
                blank=True,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="form1",
            name="temporary_contract_end_date",
            field=models.DateField(
                blank=True,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="form1",
            name="temporary_internal_gl_start_date",
            field=models.DateField(
                blank=True,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="form1",
            name="temporary_internal_gl_end_date",
            field=models.DateField(
                blank=True,
                null=True,
            ),
        ),

        migrations.RunPython(
            copy_datetime_values_to_date_fields,
            restore_date_values_to_datetime_fields,
        ),

        migrations.RemoveField(
            model_name="form1",
            name="contract_start_date",
        ),
        migrations.RemoveField(
            model_name="form1",
            name="contract_end_date",
        ),
        migrations.RemoveField(
            model_name="form1",
            name="internal_gl_start_date",
        ),
        migrations.RemoveField(
            model_name="form1",
            name="internal_gl_end_date",
        ),

        migrations.RenameField(
            model_name="form1",
            old_name="temporary_contract_start_date",
            new_name="contract_start_date",
        ),
        migrations.RenameField(
            model_name="form1",
            old_name="temporary_contract_end_date",
            new_name="contract_end_date",
        ),
        migrations.RenameField(
            model_name="form1",
            old_name="temporary_internal_gl_start_date",
            new_name="internal_gl_start_date",
        ),
        migrations.RenameField(
            model_name="form1",
            old_name="temporary_internal_gl_end_date",
            new_name="internal_gl_end_date",
        ),
    ]
