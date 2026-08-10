from decimal import Decimal

from django.db import migrations
from django.db.models import Sum


ZERO = Decimal("0.00")
BOTH = "BOTH"


def initialize_review_snapshots(apps, schema_editor):
    Form1 = apps.get_model(
        "tracking",
        "Form1",
    )
    GLExpenditure = apps.get_model(
        "tracking",
        "GLExpenditure",
    )
    FiscalBreakdown = apps.get_model(
        "tracking",
        "FiscalBreakdown",
    )
    SubsequentAdjustment = apps.get_model(
        "tracking",
        "SubsequentAdjustment",
    )
    SubsequentFiscalBreakdown = apps.get_model(
        "tracking",
        "SubsequentFiscalBreakdown",
    )

    both_grant_ids = set(
        Form1.objects.filter(
            funding_sources=BOTH
        ).values_list(
            "grant_id",
            flat=True,
        )
    )

    fiscal_initialized = 0
    fiscal_missing = 0
    fiscal_mismatch = 0
    fiscal_duplicate = 0

    fiscal_source_rows = (
        GLExpenditure.objects
        .filter(
            grant_id__in=both_grant_ids
        )
        .values(
            "grant_id",
            "fiscal_year",
        )
        .annotate(
            total=Sum("net_expenditure")
        )
    )

    for source_row in fiscal_source_rows:
        grant_id = source_row["grant_id"]
        fiscal_year = source_row["fiscal_year"]
        source_total = (
            source_row["total"] or ZERO
        )

        rows = FiscalBreakdown.objects.filter(
            grant_id_id=grant_id,
            fiscal_year=fiscal_year,
        )

        count = rows.count()

        if count == 0:
            fiscal_missing += 1
            continue

        if count > 1:
            fiscal_duplicate += 1
            continue

        breakdown = rows.first()

        federal = breakdown.federal or ZERO
        nonfederal = breakdown.nonfederal or ZERO

        if federal + nonfederal != source_total:
            fiscal_mismatch += 1
            continue

        breakdown.reviewed_total_allowed_expenditure = (
            source_total
        )
        breakdown.save(
            update_fields=[
                "reviewed_total_allowed_expenditure"
            ]
        )

        fiscal_initialized += 1

    subsequent_initialized = 0
    subsequent_missing = 0
    subsequent_mismatch = 0
    subsequent_duplicate = 0

    subsequent_source_rows = (
        SubsequentAdjustment.objects
        .filter(
            grant_id_id__in=both_grant_ids
        )
        .values(
            "grant_id_id",
            "fiscal_year",
        )
        .annotate(
            total=Sum("net_expenditure")
        )
    )

    for source_row in subsequent_source_rows:
        grant_id = source_row["grant_id_id"]
        fiscal_year = source_row["fiscal_year"]
        source_total = (
            source_row["total"] or ZERO
        )

        rows = SubsequentFiscalBreakdown.objects.filter(
            grant_id_id=grant_id,
            fiscal_year=fiscal_year,
        )

        count = rows.count()

        if count == 0:
            subsequent_missing += 1
            continue

        if count > 1:
            subsequent_duplicate += 1
            continue

        breakdown = rows.first()

        federal = breakdown.federal or ZERO
        nonfederal = breakdown.nonfederal or ZERO

        if federal + nonfederal != source_total:
            subsequent_mismatch += 1
            continue

        breakdown.reviewed_total_subsequent_adjustment = (
            source_total
        )
        breakdown.save(
            update_fields=[
                "reviewed_total_subsequent_adjustment"
            ]
        )

        subsequent_initialized += 1

    print()
    print("Allocation review snapshot initialization")
    print("=========================================")

    print()
    print("In-Period")
    print("Initialized:", fiscal_initialized)
    print("Source without breakdown:", fiscal_missing)
    print("Allocation mismatch:", fiscal_mismatch)
    print("Duplicate breakdown:", fiscal_duplicate)

    print()
    print("Subsequent Adjustment")
    print("Initialized:", subsequent_initialized)
    print("Source without breakdown:", subsequent_missing)
    print("Allocation mismatch:", subsequent_mismatch)
    print("Duplicate breakdown:", subsequent_duplicate)


def reverse_review_snapshots(apps, schema_editor):
    FiscalBreakdown = apps.get_model(
        "tracking",
        "FiscalBreakdown",
    )
    SubsequentFiscalBreakdown = apps.get_model(
        "tracking",
        "SubsequentFiscalBreakdown",
    )

    FiscalBreakdown.objects.update(
        reviewed_total_allowed_expenditure=None
    )

    SubsequentFiscalBreakdown.objects.update(
        reviewed_total_subsequent_adjustment=None
    )


class Migration(migrations.Migration):

    dependencies = [
        (
            "tracking",
            "0016_add_allocation_review_snapshots",
        ),
    ]

    operations = [
        migrations.RunPython(
            initialize_review_snapshots,
            reverse_review_snapshots,
        ),
    ]