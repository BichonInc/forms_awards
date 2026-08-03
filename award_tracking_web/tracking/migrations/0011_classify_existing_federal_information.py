import re
from collections import Counter

from django.db import migrations


SPECIAL_VALUES = {
    "",
    "none",
    "n/a",
    "na",
    "not applicable",
}

ALN_PATTERN = re.compile(r"^\d{2}\.\d{3}$")


def normalize(value):
    if value is None:
        return ""

    return str(value).strip()


def is_missing(value):
    return normalize(value).lower() in SPECIAL_VALUES


def classify_existing_federal_information(apps, schema_editor):
    Form1 = apps.get_model("tracking", "Form1")

    grants_to_update = []
    counts = Counter()

    for grant in Form1.objects.all().iterator():
        grantor = normalize(grant.federal_grantor)
        aln = normalize(grant.federal_aln)

        grantor_present = not is_missing(grantor)
        aln_present = not is_missing(aln)
        valid_aln = (
            aln_present
            and ALN_PATTERN.fullmatch(aln) is not None
        )

        if grantor_present and valid_aln:
            grant.federal_funding_included = True
            grant.federal_information_status = "COMPLETE"
            counts["complete"] += 1

        elif not grantor_present and not aln_present:
            grant.federal_funding_included = False
            grant.federal_information_status = "NOT_APPLICABLE"
            counts["not_applicable"] += 1

        else:
            grant.federal_funding_included = True
            grant.federal_information_status = "PENDING"
            counts["pending"] += 1

        grants_to_update.append(grant)

    if grants_to_update:
        Form1.objects.bulk_update(
            grants_to_update,
            [
                "federal_funding_included",
                "federal_information_status",
            ],
            batch_size=500,
        )

    total = sum(counts.values())

    print()
    print("Federal information classification completed:")
    print(f"  Complete: {counts['complete']}")
    print(f"  Pending: {counts['pending']}")
    print(
        "  Not Applicable: "
        f"{counts['not_applicable']}"
    )
    print(f"  Total classified: {total}")


def clear_federal_information_classification(apps, schema_editor):
    Form1 = apps.get_model("tracking", "Form1")

    Form1.objects.update(
        federal_funding_included=None,
        federal_information_status=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        (
            "tracking",
            "0010_add_grant_classification_fields",
        ),
    ]

    operations = [
        migrations.RunPython(
            classify_existing_federal_information,
            reverse_code=clear_federal_information_classification,
        ),
    ]