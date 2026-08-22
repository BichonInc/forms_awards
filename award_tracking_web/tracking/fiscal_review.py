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