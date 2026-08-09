from decimal import Decimal

from django.db import migrations


def cleanup_orphan_allocations(apps, schema_editor):
    FiscalBreakdown = apps.get_model(
        "tracking",
        "FiscalBreakdown",
    )
    GLExpenditure = apps.get_model(
        "tracking",
        "GLExpenditure",
    )
    SubsequentFiscalBreakdown = apps.get_model(
        "tracking",
        "SubsequentFiscalBreakdown",
    )
    SubsequentAdjustment = apps.get_model(
        "tracking",
        "SubsequentAdjustment",
    )

    fiscal_targets = [
        {
            "grant_id": "A00073",
            "fiscal_year": "FY22-23",
            "federal": Decimal("96600.00"),
            "nonfederal": Decimal("0.00"),
        },
        {
            "grant_id": "A00028",
            "fiscal_year": "FY22-23",
            "federal": Decimal("0.00"),
            "nonfederal": Decimal("0.00"),
        },
        {
            "grant_id": "A00029",
            "fiscal_year": "FY22-23",
            "federal": Decimal("0.00"),
            "nonfederal": Decimal("0.00"),
        },
    ]

    for target in fiscal_targets:
        source_exists = GLExpenditure.objects.filter(
            grant_id=target["grant_id"],
            fiscal_year=target["fiscal_year"],
        ).exists()

        rows = FiscalBreakdown.objects.filter(
            grant_id_id=target["grant_id"],
            fiscal_year=target["fiscal_year"],
            federal=target["federal"],
            nonfederal=target["nonfederal"],
        )

        count = rows.count()

        if source_exists:
            print(
                "SKIPPED fiscal cleanup:",
                target["grant_id"],
                target["fiscal_year"],
                "- matching GL source exists",
            )
        elif count == 1:
            rows.delete()

            print(
                "DELETED fiscal orphan:",
                target["grant_id"],
                target["fiscal_year"],
            )
        elif count == 0:
            print(
                "SKIPPED fiscal cleanup:",
                target["grant_id"],
                target["fiscal_year"],
                "- exact target row not found",
            )
        else:
            print(
                "SKIPPED fiscal cleanup:",
                target["grant_id"],
                target["fiscal_year"],
                "- multiple exact target rows found",
            )

    subsequent_target = {
        "grant_id": "A00062",
        "fiscal_year": "FY22-23",
        "federal": Decimal("-6359.00"),
        "nonfederal": Decimal("0.00"),
    }

    source_exists = SubsequentAdjustment.objects.filter(
        grant_id_id=subsequent_target["grant_id"],
        fiscal_year=subsequent_target["fiscal_year"],
    ).exists()

    rows = SubsequentFiscalBreakdown.objects.filter(
        grant_id_id=subsequent_target["grant_id"],
        fiscal_year=subsequent_target["fiscal_year"],
        federal=subsequent_target["federal"],
        nonfederal=subsequent_target["nonfederal"],
    )

    count = rows.count()

    if source_exists:
        print(
            "SKIPPED subsequent cleanup:",
            subsequent_target["grant_id"],
            subsequent_target["fiscal_year"],
            "- matching Subsequent Adjustment source exists",
        )
    elif count == 1:
        rows.delete()

        print(
            "DELETED subsequent orphan:",
            subsequent_target["grant_id"],
            subsequent_target["fiscal_year"],
        )
    elif count == 0:
        print(
            "SKIPPED subsequent cleanup:",
            subsequent_target["grant_id"],
            subsequent_target["fiscal_year"],
            "- exact target row not found",
        )
    else:
        print(
            "SKIPPED subsequent cleanup:",
            subsequent_target["grant_id"],
            subsequent_target["fiscal_year"],
            "- multiple exact target rows found",
        )


class Migration(migrations.Migration):

    dependencies = [
        (
            "tracking",
            "0013_add_funding_sources",
        ),
    ]

    operations = [
        migrations.RunPython(
            cleanup_orphan_allocations,
            reverse_code=migrations.RunPython.noop,
        ),
    ]