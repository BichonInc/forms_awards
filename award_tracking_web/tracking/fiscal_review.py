from decimal import Decimal

from django.db.models import Sum

from .models import (
    FiscalBreakdown,
    Form1,
    GLExpenditure,
    GrantFiscalExceptionReview,
    ProgramIncome,
    SubsequentAdjustment,
    SubsequentFiscalBreakdown,
)


ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def _money(value):
    """
    Normalize monetary values to cents for accounting comparisons.
    """
    return (value or ZERO).quantize(CENT)


def _get_fiscal_year_totals(model, grant_id):
    """
    Return current source totals by fiscal year for a grant.
    """
    rows = (
        model.objects
        .filter(grant_id=grant_id)
        .exclude(fiscal_year__isnull=True)
        .exclude(fiscal_year="")
        .values("fiscal_year")
        .annotate(
            current_total=Sum("net_expenditure")
        )
        .order_by("fiscal_year")
    )

    return {
        row["fiscal_year"]: _money(row["current_total"])
        for row in rows
    }


def get_grant_fiscal_review(grant):
    """
    Calculate the grant's current fiscal-review condition.

    This function is read-only. It does not save or modify any data.
    """

    in_period_totals = _get_fiscal_year_totals(
        GLExpenditure,
        grant.grant_id,
    )

    subsequent_totals = _get_fiscal_year_totals(
        SubsequentAdjustment,
        grant.grant_id,
    )

    total_in_period = _money(
        sum(
            in_period_totals.values(),
            ZERO,
        )
    )

    total_subsequent = _money(
        sum(
            subsequent_totals.values(),
            ZERO,
        )
    )

    contract_total_allowed_expenditure = _money(
        total_in_period
        + total_subsequent
    )

    total_program_income = _money(
        ProgramIncome.objects.filter(
            grant=grant
        ).aggregate(
            total=Sum("amount")
        )["total"]
    )

    contract_amount = _money(
        grant.contract_amount
    )

    if (
        grant.program_income_treatment
        == Form1.ProgramIncomeTreatment.ADDITIVE
    ):
        contract_balance = _money(
            contract_amount
            + total_program_income
            - contract_total_allowed_expenditure
        )
    else:
        contract_balance = _money(
            contract_amount
            - contract_total_allowed_expenditure
        )

    reasons = []

    # ---------------------------------------------------------
    # Program Income Treatment
    # ---------------------------------------------------------

    program_income_treatment_needs_review = (
        total_program_income != ZERO
        and grant.program_income_treatment
        == Form1.ProgramIncomeTreatment.NOT_RESEARCHED
    )

    if program_income_treatment_needs_review:
        reasons.append(
            {
                "code": "PROGRAM_INCOME_TREATMENT",
                "message": (
                    "Program Income exists, but its treatment "
                    "has not been researched."
                ),
            }
        )

    # ---------------------------------------------------------
    # Negative Contract Balance
    # ---------------------------------------------------------

    current_negative_balance_exception = None

    if contract_balance < ZERO:
        current_negative_balance_exception = (
            GrantFiscalExceptionReview.objects.filter(
                grant=grant,
                exception_type=(
                    GrantFiscalExceptionReview
                    .ExceptionType
                    .NEGATIVE_CONTRACT_BALANCE
                ),
                reviewed_contract_amount=contract_amount,
                reviewed_total_allowed_expenditure=(
                    contract_total_allowed_expenditure
                ),
                reviewed_program_income=total_program_income,
                reviewed_program_income_treatment=(
                    grant.program_income_treatment
                ),
                reviewed_contract_balance=contract_balance,
            )
            .first()
        )

        if current_negative_balance_exception is None:
            reasons.append(
                {
                    "code": "NEGATIVE_CONTRACT_BALANCE",
                    "message": (
                        "Contract Balance is negative and the "
                        "current condition has not been accepted "
                        "with an explanation."
                    ),
                }
            )

    # ---------------------------------------------------------
    # In-Period allocation review
    # ---------------------------------------------------------

    in_period_allocation_issues = []

    if grant.funding_sources == Form1.FundingSource.BOTH:
        in_period_records = {
            record.fiscal_year: record
            for record in FiscalBreakdown.objects.filter(
                grant_id=grant,
                fiscal_year__in=in_period_totals.keys(),
            )
        }

        for fiscal_year, current_total in in_period_totals.items():
            record = in_period_records.get(
                fiscal_year
            )

            reviewed_total = (
                record.reviewed_total_allowed_expenditure
                if record
                else None
            )

            reviewed_total_normalized = (
                _money(reviewed_total)
                if reviewed_total is not None
                else None
            )

            if (
                reviewed_total_normalized is None
                or reviewed_total_normalized != current_total
            ):
                in_period_allocation_issues.append(
                    {
                        "fiscal_year": fiscal_year,
                        "current_total": current_total,
                        "reviewed_total": (
                            reviewed_total_normalized
                        ),
                    }
                )

    if in_period_allocation_issues:
        reasons.append(
            {
                "code": "IN_PERIOD_ALLOCATION",
                "message": (
                    "One or more In-Period allocations "
                    "require Accountant review."
                ),
            }
        )

    # ---------------------------------------------------------
    # Subsequent Adjustment allocation review
    # ---------------------------------------------------------

    subsequent_allocation_issues = []

    if grant.funding_sources == Form1.FundingSource.BOTH:
        subsequent_records = {
            record.fiscal_year: record
            for record
            in SubsequentFiscalBreakdown.objects.filter(
                grant_id=grant,
                fiscal_year__in=subsequent_totals.keys(),
            )
        }

        for fiscal_year, current_total in subsequent_totals.items():
            record = subsequent_records.get(
                fiscal_year
            )

            reviewed_total = (
                record.reviewed_total_subsequent_adjustment
                if record
                else None
            )

            reviewed_total_normalized = (
                _money(reviewed_total)
                if reviewed_total is not None
                else None
            )

            if (
                reviewed_total_normalized is None
                or reviewed_total_normalized != current_total
            ):
                subsequent_allocation_issues.append(
                    {
                        "fiscal_year": fiscal_year,
                        "current_total": current_total,
                        "reviewed_total": (
                            reviewed_total_normalized
                        ),
                    }
                )

    if subsequent_allocation_issues:
        reasons.append(
            {
                "code": "SUBSEQUENT_ALLOCATION",
                "message": (
                    "One or more Subsequent Adjustment "
                    "allocations require Accountant review."
                ),
            }
        )

    return {
        "needs_attention": bool(reasons),
        "reasons": reasons,
        "total_in_period": total_in_period,
        "total_subsequent": total_subsequent,
        "contract_total_allowed_expenditure": (
            contract_total_allowed_expenditure
        ),
        "total_program_income": total_program_income,
        "contract_balance": contract_balance,
        "program_income_treatment_needs_review": (
            program_income_treatment_needs_review
        ),
        "current_negative_balance_exception": (
            current_negative_balance_exception
        ),
        "in_period_allocation_issues": (
            in_period_allocation_issues
        ),
        "subsequent_allocation_issues": (
            subsequent_allocation_issues
        ),
    }


def get_grant_fiscal_review_summaries(grants):
    """
    Return compact Fiscal Review results for multiple grants.

    This is intended for list/dashboard views where calling
    get_grant_fiscal_review() separately for every grant would
    create excessive database queries.
    """
    grants = list(grants)

    if not grants:
        return {}

    grant_ids = [
        grant.grant_id
        for grant in grants
    ]

    def get_current_totals(model):
        rows = (
            model.objects
            .filter(grant_id__in=grant_ids)
            .exclude(fiscal_year__isnull=True)
            .exclude(fiscal_year="")
            .values(
                "grant_id",
                "fiscal_year",
            )
            .annotate(
                current_total=Sum("net_expenditure")
            )
        )

        totals = {}

        for row in rows:
            grant_id = row["grant_id"]
            fiscal_year = row["fiscal_year"]

            totals.setdefault(
                grant_id,
                {},
            )[fiscal_year] = _money(
                row["current_total"]
            )

        return totals

    in_period_totals = get_current_totals(
        GLExpenditure
    )

    subsequent_totals = get_current_totals(
        SubsequentAdjustment
    )

    program_income_totals = {
        row["grant_id"]: _money(row["total"])
        for row in (
            ProgramIncome.objects
            .filter(grant_id__in=grant_ids)
            .values("grant_id")
            .annotate(total=Sum("amount"))
        )
    }

    both_grant_ids = [
        grant.grant_id
        for grant in grants
        if grant.funding_sources
        == Form1.FundingSource.BOTH
    ]

    in_period_snapshots = {}

    for row in (
        FiscalBreakdown.objects
        .filter(
            grant_id__grant_id__in=both_grant_ids
        )
        .values(
            "grant_id__grant_id",
            "fiscal_year",
            "reviewed_total_allowed_expenditure",
        )
    ):
        reviewed_total = row[
            "reviewed_total_allowed_expenditure"
        ]

        in_period_snapshots[
            (
                row["grant_id__grant_id"],
                row["fiscal_year"],
            )
        ] = (
            _money(reviewed_total)
            if reviewed_total is not None
            else None
        )

    subsequent_snapshots = {}

    for row in (
        SubsequentFiscalBreakdown.objects
        .filter(
            grant_id__grant_id__in=both_grant_ids
        )
        .values(
            "grant_id__grant_id",
            "fiscal_year",
            "reviewed_total_subsequent_adjustment",
        )
    ):
        reviewed_total = row[
            "reviewed_total_subsequent_adjustment"
        ]

        subsequent_snapshots[
            (
                row["grant_id__grant_id"],
                row["fiscal_year"],
            )
        ] = (
            _money(reviewed_total)
            if reviewed_total is not None
            else None
        )

    accepted_negative_balance_snapshots = set()

    for row in (
        GrantFiscalExceptionReview.objects
        .filter(
            grant_id__in=grant_ids,
            exception_type=(
                GrantFiscalExceptionReview
                .ExceptionType
                .NEGATIVE_CONTRACT_BALANCE
            ),
        )
        .values(
            "grant_id",
            "reviewed_contract_amount",
            "reviewed_total_allowed_expenditure",
            "reviewed_program_income",
            "reviewed_program_income_treatment",
            "reviewed_contract_balance",
        )
    ):
        accepted_negative_balance_snapshots.add(
            (
                row["grant_id"],
                _money(
                    row["reviewed_contract_amount"]
                ),
                _money(
                    row[
                        "reviewed_total_allowed_expenditure"
                    ]
                ),
                _money(
                    row["reviewed_program_income"]
                ),
                row[
                    "reviewed_program_income_treatment"
                ],
                _money(
                    row["reviewed_contract_balance"]
                ),
            )
        )

    summaries = {}

    for grant in grants:
        grant_id = grant.grant_id

        current_in_period = (
            in_period_totals.get(
                grant_id,
                {},
            )
        )

        current_subsequent = (
            subsequent_totals.get(
                grant_id,
                {},
            )
        )

        total_in_period = _money(
            sum(
                current_in_period.values(),
                ZERO,
            )
        )

        total_subsequent = _money(
            sum(
                current_subsequent.values(),
                ZERO,
            )
        )

        total_allowed_expenditure = _money(
            total_in_period
            + total_subsequent
        )

        total_program_income = (
            program_income_totals.get(
                grant_id,
                ZERO,
            )
        )

        contract_amount = _money(
            grant.contract_amount
        )

        if (
            grant.program_income_treatment
            == Form1.ProgramIncomeTreatment.ADDITIVE
        ):
            contract_balance = _money(
                contract_amount
                + total_program_income
                - total_allowed_expenditure
            )
        else:
            contract_balance = _money(
                contract_amount
                - total_allowed_expenditure
            )

        reason_codes = []

        if (
            total_program_income != ZERO
            and grant.program_income_treatment
            == Form1.ProgramIncomeTreatment.NOT_RESEARCHED
        ):
            reason_codes.append(
                "PROGRAM_INCOME_TREATMENT"
            )

        if contract_balance < ZERO:
            current_snapshot = (
                grant_id,
                contract_amount,
                total_allowed_expenditure,
                total_program_income,
                grant.program_income_treatment,
                contract_balance,
            )

            if (
                current_snapshot
                not in accepted_negative_balance_snapshots
            ):
                reason_codes.append(
                    "NEGATIVE_CONTRACT_BALANCE"
                )

        if (
            grant.funding_sources
            == Form1.FundingSource.BOTH
        ):
            in_period_needs_review = any(
                in_period_snapshots.get(
                    (
                        grant_id,
                        fiscal_year,
                    )
                )
                != current_total
                for fiscal_year, current_total
                in current_in_period.items()
            )

            if in_period_needs_review:
                reason_codes.append(
                    "IN_PERIOD_ALLOCATION"
                )

            subsequent_needs_review = any(
                subsequent_snapshots.get(
                    (
                        grant_id,
                        fiscal_year,
                    )
                )
                != current_total
                for fiscal_year, current_total
                in current_subsequent.items()
            )

            if subsequent_needs_review:
                reason_codes.append(
                    "SUBSEQUENT_ALLOCATION"
                )

        summaries[grant_id] = {
            "needs_attention": bool(
                reason_codes
            ),
            "issue_count": len(
                reason_codes
            ),
            "reason_codes": reason_codes,
        }

    return summaries
