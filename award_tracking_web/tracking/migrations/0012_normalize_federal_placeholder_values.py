from django.db import migrations


SPECIAL_VALUES = {
    "",
    "none",
    "n/a",
    "na",
    "not applicable",
}


def is_placeholder(value):
    if value is None:
        return False

    return str(value).strip().lower() in SPECIAL_VALUES


def normalize_federal_placeholder_values(apps, schema_editor):
    Form1 = apps.get_model("tracking", "Form1")
    database_alias = schema_editor.connection.alias

    grants_to_update = []
    grantor_values_cleared = 0
    aln_values_cleared = 0
    records_updated = 0

    grants = (
        Form1.objects
        .using(database_alias)
        .all()
        .iterator()
    )

    for grant in grants:
        changed = False

        if is_placeholder(grant.federal_grantor):
            grant.federal_grantor = None
            grantor_values_cleared += 1
            changed = True

        if is_placeholder(grant.federal_aln):
            grant.federal_aln = None
            aln_values_cleared += 1
            changed = True

        if changed:
            grants_to_update.append(grant)
            records_updated += 1

    if grants_to_update:
        Form1.objects.using(database_alias).bulk_update(
            grants_to_update,
            [
                "federal_grantor",
                "federal_aln",
            ],
            batch_size=500,
        )

    print()
    print("Federal placeholder cleanup completed:")
    print(
        "  Federal Grantor values cleared: "
        f"{grantor_values_cleared}"
    )
    print(
        "  Federal ALN values cleared: "
        f"{aln_values_cleared}"
    )
    print(f"  Records updated: {records_updated}")


class Migration(migrations.Migration):

    dependencies = [
        (
            "tracking",
            "0011_classify_existing_federal_information",
        ),
    ]

    operations = [
        migrations.RunPython(
            normalize_federal_placeholder_values,
            reverse_code=migrations.RunPython.noop,
        ),
    ]