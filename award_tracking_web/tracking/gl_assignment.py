from collections import defaultdict
from datetime import datetime

from .models import Form1, GLExpenditure


def _normalize_award_code(value):
    if value is None:
        return ""

    return str(value).strip()


def _as_date(value):
    """
    Normalize DateField/DateTimeField values for inclusive date comparison.
    """
    if isinstance(value, datetime):
        return value.date()

    return value


def rematch_gl_expenditures(*, award_codes=None, dry_run=False):
    """
    Re-evaluate GLExpenditure.grant_id using the authoritative Form1
    Award Code + Internal GL date-range rules.

    If award_codes is None, all GL rows are evaluated. This is appropriate
    after a full GL refresh.

    If award_codes is provided, only GL rows for those Award Codes are
    evaluated. This is appropriate after an approved Form1 change.

    fiscal_year and all imported GL transaction values are left unchanged.
    Only grant_id may be updated.
    """

    if award_codes is None:
        expenditures = list(
            GLExpenditure.objects
            .all()
            .order_by("id")
        )

        grants = list(
            Form1.objects
            .only(
                "grant_id",
                "internal_award_code",
                "internal_gl_start_date",
                "internal_gl_end_date",
            )
        )

    else:
        normalized_codes = {
            _normalize_award_code(code)
            for code in award_codes
            if _normalize_award_code(code)
        }

        if not normalized_codes:
            return {
                "examined": 0,
                "changed": 0,
                "assigned": 0,
                "unassigned": 0,
                "dry_run": dry_run,
            }

        expenditures = list(
            GLExpenditure.objects
            .filter(award_code__in=normalized_codes)
            .order_by("id")
        )

        grants = list(
            Form1.objects
            .filter(
                internal_award_code__in=normalized_codes
            )
            .only(
                "grant_id",
                "internal_award_code",
                "internal_gl_start_date",
                "internal_gl_end_date",
            )
        )

    grants_by_award_code = defaultdict(list)

    for grant in grants:
        award_code = _normalize_award_code(
            grant.internal_award_code
        )

        if award_code:
            grants_by_award_code[award_code].append(grant)

    changed_expenditures = []
    assigned_count = 0
    unassigned_count = 0

    for expenditure in expenditures:
        award_code = _normalize_award_code(
            expenditure.award_code
        )
        effective_date = _as_date(
            expenditure.effective_date
        )

        matching_grants = []

        for grant in grants_by_award_code.get(
            award_code,
            [],
        ):
            start_date = _as_date(
                grant.internal_gl_start_date
            )
            end_date = _as_date(
                grant.internal_gl_end_date
            )

            if (
                effective_date is not None
                and start_date is not None
                and end_date is not None
                and start_date <= effective_date <= end_date
            ):
                matching_grants.append(grant)

        if len(matching_grants) > 1:
            conflicting_grant_ids = ", ".join(
                sorted(
                    grant.grant_id
                    for grant in matching_grants
                )
            )

            raise ValueError(
                (
                    "Ambiguous GL assignment for Award Code "
                    f"{award_code}, Effective Date "
                    f"{effective_date}: matching grants "
                    f"{conflicting_grant_ids}."
                )
            )

        if matching_grants:
            new_grant_id = matching_grants[0].grant_id
            assigned_count += 1
        else:
            new_grant_id = None
            unassigned_count += 1

        if expenditure.grant_id != new_grant_id:
            expenditure.grant_id = new_grant_id
            changed_expenditures.append(expenditure)

    if changed_expenditures and not dry_run:
        GLExpenditure.objects.bulk_update(
            changed_expenditures,
            ["grant_id"],
            batch_size=1000,
        )

    return {
        "examined": len(expenditures),
        "changed": len(changed_expenditures),
        "assigned": assigned_count,
        "unassigned": unassigned_count,
        "dry_run": dry_run,
    }