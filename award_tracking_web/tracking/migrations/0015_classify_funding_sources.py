from collections import Counter

from django.db import migrations


FEDERAL = "FEDERAL"
NONFEDERAL = "NONFEDERAL"
BOTH = "BOTH"
REVIEW_REQUIRED = "REVIEW_REQUIRED"


def has_text(value):
    return bool(
        value
        and str(value).strip()
    )


def is_direct_federal_agency(value):
    if not has_text(value):
        return False

    return (
        str(value)
        .strip()
        .lower()
        .startswith("u.s. department of")
    )


def classify_funding_sources(apps, schema_editor):
    Form1 = apps.get_model(
        "tracking",
        "Form1",
    )
    FiscalBreakdown = apps.get_model(
        "tracking",
        "FiscalBreakdown",
    )
    SubsequentFiscalBreakdown = apps.get_model(
        "tracking",
        "SubsequentFiscalBreakdown",
    )

    federal_grant_ids = set(
        FiscalBreakdown.objects
        .exclude(federal=0)
        .values_list(
            "grant_id_id",
            flat=True,
        )
    )

    federal_grant_ids.update(
        SubsequentFiscalBreakdown.objects
        .exclude(federal=0)
        .values_list(
            "grant_id_id",
            flat=True,
        )
    )

    nonfederal_grant_ids = set(
        FiscalBreakdown.objects
        .exclude(nonfederal=0)
        .values_list(
            "grant_id_id",
            flat=True,
        )
    )

    nonfederal_grant_ids.update(
        SubsequentFiscalBreakdown.objects
        .exclude(nonfederal=0)
        .values_list(
            "grant_id_id",
            flat=True,
        )
    )

    counts = Counter()

    for grant in Form1.objects.all():
        grant_id = grant.grant_id

        federal_exists = (
            grant_id in federal_grant_ids
        )
        nonfederal_exists = (
            grant_id in nonfederal_grant_ids
        )

        federal_grantor_exists = has_text(
            grant.federal_grantor
        )

        direct_federal = (
            is_direct_federal_agency(
                grant.contracting_agency
            )
        )

        if direct_federal:
            classification = FEDERAL

        elif not federal_grantor_exists:
            if federal_exists:
                classification = REVIEW_REQUIRED
            else:
                classification = NONFEDERAL

        elif nonfederal_exists:
            classification = BOTH

        else:
            # Federal Grantor exists, but either:
            #
            # 1. Federal allocation exists and no
            #    Non-federal allocation has yet
            #    been observed; or
            #
            # 2. No meaningful allocation history
            #    exists.
            #
            # Neither condition proves that the
            # contract is 100% Federal.
            classification = REVIEW_REQUIRED

        Form1.objects.filter(
            pk=grant.pk
        ).update(
            funding_sources=classification
        )

        counts[classification] += 1

    print()
    print("Funding source classification complete")
    print(
        "FEDERAL:",
        counts[FEDERAL],
    )
    print(
        "NONFEDERAL:",
        counts[NONFEDERAL],
    )
    print(
        "BOTH:",
        counts[BOTH],
    )
    print(
        "REVIEW_REQUIRED:",
        counts[REVIEW_REQUIRED],
    )
    print(
        "TOTAL:",
        sum(counts.values()),
    )


def reverse_funding_sources(apps, schema_editor):
    Form1 = apps.get_model(
        "tracking",
        "Form1",
    )

    Form1.objects.update(
        funding_sources=None
    )


class Migration(migrations.Migration):

    dependencies = [
        (
            "tracking",
            "0014_cleanup_orphan_allocations",
        ),
    ]

    operations = [
        migrations.RunPython(
            classify_funding_sources,
            reverse_funding_sources,
        ),
    ]