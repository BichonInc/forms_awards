from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.db import IntegrityError, transaction
from django.db.models import Sum, Max
from django.core import signing
from django.utils import timezone
from .models import (
    Form1,
    GLExpenditure,
    FiscalBreakdown,
    SubsequentAdjustment,
    SubsequentFiscalBreakdown,
    ProgramIncome,
    GrantFiscalExceptionReview,
    ChangeRequest,
    ChangeRequestField,
    ChangeAction,
    CHANGE_REQUEST_BLOCKING_STATUSES,
    CHANGE_REQUEST_SUBMITTED_STATUSES,
)
from .fiscal_review import (
    get_grant_fiscal_review,
    get_grant_fiscal_review_summaries,
)
from .gl_assignment import rematch_gl_expenditures
from .permissions import (
    ROLE_ACCOUNTANT,
    ROLE_ADMINISTRATOR,
    ROLE_APPROVER,
    ROLE_EDITOR,
    role_required,
    user_has_any_role,
)
from .forms import (
    GRANT_BASIC_INFORMATION_CHANGE_FIELDS,
    GrantBasicInformationChangeForm,
    GrantForm,
)
from .change_request_workflow import (
    serialize_change_request_value,
)
from django.core.files.storage import default_storage
import pandas as pd
from datetime import datetime, date
from decimal import Decimal, InvalidOperation # Import this to ensure consistent types
from django.conf import settings
import os
import logging
import csv
from django.http import HttpResponse

# Function to generate new grant_id
#def generate_new_grant_id():
#   last_grant = Form1.objects.aggregate(last_id=Max('grant_id'))
#    last_id = last_grant['last_id']
#    if last_id:
#       numeric_part = int(last_id[1:]) + 1
#        new_grant_id = f'A{numeric_part:05d}'
#    else:
#        new_grant_id = 'A00001'
#    return new_grant_id


# In views.py
@login_required
def grant_list(request):
    grants = list(
        Form1.objects.all().order_by("grant_id")
    )

    fiscal_review_summaries = (
        get_grant_fiscal_review_summaries(grants)
    )

    for grant in grants:
        fiscal_summary = fiscal_review_summaries.get(
            grant.grant_id,
            {
                "needs_attention": False,
                "issue_count": 0,
                "reason_codes": [],
            },
        )

        grant.fiscal_review_issue_count = (
            fiscal_summary["issue_count"]
        )

    user_roles = list(
        request.user.groups.order_by("name").values_list(
            "name",
            flat=True,
        )
    )

    context = {
        'grants': grants,
        "can_create_grant": user_has_any_role(
            request.user,
            ROLE_EDITOR,
        ),
        "can_refresh_financial_data": user_has_any_role(
            request.user,
            ROLE_ACCOUNTANT,
        ),
        "user_roles": user_roles,
    }
    return render(request, 'tracking/grant_list.html', context)


def format_change_request_display_value(field_name, value):
    if value in (None, ""):
        return "—"

    if field_name in {
        "contract_start_date",
        "contract_end_date",
        "internal_gl_start_date",
        "internal_gl_end_date",
    }:
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return value

    if field_name == "contract_amount":
        try:
            return f"{Decimal(value):,.2f}"
        except (InvalidOperation, TypeError, ValueError):
            return value

    if field_name == "funding_sources":
        return dict(Form1.FundingSource.choices).get(
            value,
            value,
        )

    if field_name == "federal_information_status":
        return dict(
            Form1.FederalInformationStatus.choices
        ).get(
            value,
            value,
        )

    return value


@login_required
def grant_detail(request, grant_id):
    print(f"Grant detail accessed for grant_id: {grant_id}")

    # Fetch the grant object
    grant = get_object_or_404(Form1, grant_id=grant_id)
    print(f"Grant object fetched: {grant}")

    fiscal_review = get_grant_fiscal_review(grant)

    has_fiscal_exception_history = (
        GrantFiscalExceptionReview.objects
        .filter(
            grant=grant,
            exception_type=(
                GrantFiscalExceptionReview
                .ExceptionType
                .NEGATIVE_CONTRACT_BALANCE
            ),
        )
        .exists()
    )

    can_request_change = user_has_any_role(
        request.user,
        ROLE_EDITOR,
    )

    can_review_change_request = user_has_any_role(
        request.user,
        ROLE_APPROVER,
    )

    active_change_request_statuses = (
        CHANGE_REQUEST_BLOCKING_STATUSES
        if can_request_change
        else CHANGE_REQUEST_SUBMITTED_STATUSES
    )

    active_change_request = (
        ChangeRequest.objects
        .filter(
            grant_id=grant_id,
            status__in=active_change_request_statuses,
        )
        .order_by("-submitted_at", "-id")
        .first()
    )

    can_edit_fiscal_data = user_has_any_role(
        request.user,
        ROLE_ACCOUNTANT,
    )

    program_income_fiscal_years = sorted(
        set(
            GLExpenditure.objects.filter(
                grant_id=grant_id
            ).exclude(
                fiscal_year__isnull=True
            ).exclude(
                fiscal_year=""
            ).values_list(
                "fiscal_year",
                flat=True,
            )
        )
        | set(
            SubsequentAdjustment.objects.filter(
                grant_id=grant_id
            ).exclude(
                fiscal_year__isnull=True
            ).exclude(
                fiscal_year=""
            ).values_list(
                "fiscal_year",
                flat=True,
            )
        )
        | set(
            ProgramIncome.objects.filter(
                grant=grant
            ).exclude(
                fiscal_year__isnull=True
            ).exclude(
                fiscal_year=""
            ).values_list(
                "fiscal_year",
                flat=True,
            )
        )
    )

    if request.method == 'POST':
        form_type = request.POST.get("form_type")

        print(f"Form type received: {form_type}")

        if form_type == "basic":
            if not can_request_change:
                raise PermissionDenied

            messages.info(
                request,
                (
                    "Direct changes to Grant Basic Information "
                    "are no longer permitted. Basic Information "
                    "changes must be submitted through the "
                    "change-request workflow."
                ),
            )
            return redirect(
                "grant_detail",
                grant_id=grant_id,
            )

        elif form_type == "program_income":
            if not can_edit_fiscal_data:
                raise PermissionDenied

            print("Program Income form submission detected")

            treatment = request.POST.get(
                "program_income_treatment",
                "",
            ).strip()

            valid_treatments = {
                value
                for value, label
                in Form1.ProgramIncomeTreatment.choices
            }

            if treatment not in valid_treatments:
                messages.error(
                    request,
                    "Please select a valid Program Income Treatment.",
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            parsed_amounts = []

            try:
                for i, fiscal_year in enumerate(
                    program_income_fiscal_years,
                    start=1,
                ):
                    raw_amount = request.POST.get(
                        f"program_income_{i}",
                        "",
                    ).replace(
                        ",",
                        "",
                    ).strip()

                    amount = (
                        Decimal(raw_amount)
                        if raw_amount
                        else Decimal("0.00")
                    )

                    if not amount.is_finite():
                        raise InvalidOperation

                    parsed_amounts.append(
                        (
                            fiscal_year,
                            amount,
                        )
                    )

            except InvalidOperation:
                messages.error(
                    request,
                    (
                        "Program Income amounts must "
                        "be valid numbers."
                    ),
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            with transaction.atomic():
                grant.program_income_treatment = treatment
                grant.save(
                    update_fields=[
                        "program_income_treatment",
                    ]
                )

                for fiscal_year, amount in parsed_amounts:
                    if amount == Decimal("0.00"):
                        ProgramIncome.objects.filter(
                            grant=grant,
                            fiscal_year=fiscal_year,
                        ).delete()
                    else:
                        ProgramIncome.objects.update_or_create(
                            grant=grant,
                            fiscal_year=fiscal_year,
                            defaults={
                                "amount": amount,
                            },
                        )

            messages.success(
                request,
                "Program Income information was saved successfully.",
            )
            return redirect(
                "grant_detail",
                grant_id=grant_id,
            )

        elif form_type == "accept_fiscal_exception":
            if not can_edit_fiscal_data:
                raise PermissionDenied

            explanation = request.POST.get(
                "exception_explanation",
                "",
            ).strip()

            if not explanation:
                messages.error(
                    request,
                    (
                        "An explanation is required to accept "
                        "a negative Contract Balance."
                    ),
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            fiscal_review = get_grant_fiscal_review(grant)

            if fiscal_review["contract_balance"] >= Decimal("0.00"):
                messages.error(
                    request,
                    (
                        "The Contract Balance is no longer negative, "
                        "so there is no exception to accept."
                    ),
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            if (
                fiscal_review["current_negative_balance_exception"]
                is not None
            ):
                messages.info(
                    request,
                    (
                        "The current negative Contract Balance "
                        "has already been accepted."
                    ),
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            GrantFiscalExceptionReview.objects.create(
                grant=grant,
                exception_type=(
                    GrantFiscalExceptionReview
                    .ExceptionType
                    .NEGATIVE_CONTRACT_BALANCE
                ),
                reviewed_contract_amount=grant.contract_amount,
                reviewed_total_allowed_expenditure=(
                    fiscal_review[
                        "contract_total_allowed_expenditure"
                    ]
                ),
                reviewed_program_income=(
                    fiscal_review["total_program_income"]
                ),
                reviewed_program_income_treatment=(
                    grant.program_income_treatment
                ),
                reviewed_contract_balance=(
                    fiscal_review["contract_balance"]
                ),
                explanation=explanation,
                accepted_by=request.user,
            )

            messages.success(
                request,
                (
                    "The current negative Contract Balance "
                    "was accepted with an explanation."
                ),
            )

            return redirect(
                "grant_detail",
                grant_id=grant_id,
            )

        elif form_type == "revise_fiscal_exception":
            if not can_edit_fiscal_data:
                raise PermissionDenied

            explanation = request.POST.get(
                "exception_explanation",
                "",
            ).strip()

            if not explanation:
                messages.error(
                    request,
                    (
                        "An explanation is required to revise "
                        "the accepted negative Contract Balance."
                    ),
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            fiscal_review = get_grant_fiscal_review(grant)

            if fiscal_review["contract_balance"] >= Decimal("0.00"):
                messages.error(
                    request,
                    (
                        "The Contract Balance is no longer negative, "
                        "so there is no current exception to revise."
                    ),
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            current_exception = fiscal_review[
                "current_negative_balance_exception"
            ]

            if current_exception is None:
                messages.error(
                    request,
                    (
                        "The current negative Contract Balance "
                        "has not yet been accepted. Please use "
                        "Accept with Explanation instead."
                    ),
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            GrantFiscalExceptionReview.objects.create(
                grant=grant,
                exception_type=(
                    GrantFiscalExceptionReview
                    .ExceptionType
                    .NEGATIVE_CONTRACT_BALANCE
                ),
                reviewed_contract_amount=grant.contract_amount,
                reviewed_total_allowed_expenditure=(
                    fiscal_review[
                        "contract_total_allowed_expenditure"
                    ]
                ),
                reviewed_program_income=(
                    fiscal_review["total_program_income"]
                ),
                reviewed_program_income_treatment=(
                    grant.program_income_treatment
                ),
                reviewed_contract_balance=(
                    fiscal_review["contract_balance"]
                ),
                explanation=explanation,
                accepted_by=request.user,
            )

            messages.success(
                request,
                "The accepted explanation was revised successfully.",
            )

            return redirect(
                "grant_detail",
                grant_id=grant_id,
            )

        elif form_type == "fiscal":

            if not can_edit_fiscal_data:
                raise PermissionDenied

            print("Fiscal form submission detected")

            if grant.funding_sources != Form1.FundingSource.BOTH:
                messages.error(
                    request,
                    (
                        "Manual In-Period allocation is only available "
                        "for grants funded by both Federal and "
                        "Non-federal sources."
                    ),
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            fiscal_breakdown = (
                GLExpenditure.objects
                .filter(grant_id=grant_id)
                .exclude(fiscal_year__isnull=True)
                .exclude(fiscal_year="")
                .values("fiscal_year")
                .annotate(
                    total_expenditure=Sum("net_expenditure")
                )
                .order_by("fiscal_year")
            )

            parsed_allocations = []

            try:
                for i, breakdown in enumerate(
                        fiscal_breakdown,
                        start=1,
                ):
                    fiscal_year = breakdown["fiscal_year"]

                    total_expenditure = (
                            breakdown["total_expenditure"]
                            or Decimal("0.00")
                    )

                    raw_federal = request.POST.get(
                        f"federal_{i}",
                        "",
                    ).replace(
                        ",",
                        "",
                    ).strip()

                    federal_input = (
                        Decimal(raw_federal)
                        if raw_federal
                        else Decimal("0.00")
                    )

                    if not federal_input.is_finite():
                        raise InvalidOperation

                    nonfederal_calculated = (
                            total_expenditure
                            - federal_input
                    )

                    parsed_allocations.append(
                        (
                            fiscal_year,
                            total_expenditure,
                            federal_input,
                            nonfederal_calculated,
                        )
                    )

            except InvalidOperation:
                messages.error(
                    request,
                    "Federal allocation amounts must be valid numbers.",
                )
                return redirect(
                    "grant_detail",
                    grant_id=grant_id,
                )

            with transaction.atomic():
                for (
                        fiscal_year,
                        total_expenditure,
                        federal_input,
                        nonfederal_calculated,
                ) in parsed_allocations:
                    FiscalBreakdown.objects.update_or_create(
                        grant_id=grant,
                        fiscal_year=fiscal_year,
                        defaults={
                            "federal": federal_input,
                            "nonfederal": nonfederal_calculated,
                            "reviewed_total_allowed_expenditure": (
                                total_expenditure
                            ),
                        },
                    )

            messages.success(
                request,
                "In-Period allocation was saved successfully.",
            )
            return redirect(
                "grant_detail",
                grant_id=grant_id,
            )


        elif form_type == "subsequent":

            if not can_edit_fiscal_data:
                raise PermissionDenied

            print("Subsequent Adjustment form submission detected")

            if grant.funding_sources != Form1.FundingSource.BOTH:
                messages.error(

                    request,

                    (

                        "Manual Subsequent Adjustment allocation is only "

                        "available for grants funded by both Federal and "

                        "Non-federal sources."

                    ),

                )

                return redirect(

                    "grant_detail",

                    grant_id=grant_id,

                )

            subsequent_breakdown = (

                SubsequentAdjustment.objects

                .filter(grant_id=grant_id)

                .exclude(fiscal_year__isnull=True)

                .exclude(fiscal_year="")

                .values("fiscal_year")

                .annotate(

                    total_adjustment=Sum("net_expenditure")

                )

                .order_by("fiscal_year")

            )

            parsed_allocations = []

            try:

                for i, breakdown in enumerate(

                        subsequent_breakdown,

                        start=1,

                ):

                    fiscal_year = breakdown["fiscal_year"]

                    total_adjustment = (

                            breakdown["total_adjustment"]

                            or Decimal("0.00")

                    )

                    raw_federal = request.POST.get(

                        f"federal_subsequent_{i}",

                        "",

                    ).replace(

                        ",",

                        "",

                    ).strip()

                    federal_input = (

                        Decimal(raw_federal)

                        if raw_federal

                        else Decimal("0.00")

                    )

                    if not federal_input.is_finite():
                        raise InvalidOperation

                    nonfederal_calculated = (

                            total_adjustment

                            - federal_input

                    )

                    parsed_allocations.append(

                        (

                            fiscal_year,

                            total_adjustment,

                            federal_input,

                            nonfederal_calculated,

                        )

                    )


            except InvalidOperation:

                messages.error(

                    request,

                    (

                        "Federal Subsequent Adjustment allocation "

                        "amounts must be valid numbers."

                    ),

                )

                return redirect(

                    "grant_detail",

                    grant_id=grant_id,

                )

            with transaction.atomic():

                for (

                        fiscal_year,

                        total_adjustment,

                        federal_input,

                        nonfederal_calculated,

                ) in parsed_allocations:
                    SubsequentFiscalBreakdown.objects.update_or_create(

                        grant_id=grant,

                        fiscal_year=fiscal_year,

                        defaults={

                            "federal": federal_input,

                            "nonfederal": nonfederal_calculated,

                            "reviewed_total_subsequent_adjustment": (

                                total_adjustment

                            ),

                        },

                    )

            messages.success(

                request,

                "Subsequent Adjustment allocation was saved successfully.",

            )

            return redirect(

                "grant_detail",

                grant_id=grant_id,

            )

        else:
            messages.error(
                request,
                "The submitted form type was not recognized.",
            )
            return redirect(
                "grant_detail",
                grant_id=grant_id,
            )
    # GET request or POST data handling complete
    form = GrantForm(instance=grant)

    # Fetch fiscal year breakdown for GL Expenditure
    gl_expenditures = (
        GLExpenditure.objects
        .filter(grant_id=grant_id)
        .exclude(fiscal_year__isnull=True)
        .exclude(fiscal_year="")
    )

    fiscal_breakdown = (
        gl_expenditures
        .values("fiscal_year")
        .annotate(
            total_expenditure=Sum("net_expenditure")
        )
        .order_by("fiscal_year")
    )

    total_expenditure_sum = Decimal('0')
    total_federal_sum = Decimal('0')
    total_nonfederal_sum = Decimal('0')
    total_difference = Decimal('0')

    for breakdown in fiscal_breakdown:
        fiscal_year = breakdown["fiscal_year"]

        total_expenditure = (
                breakdown["total_expenditure"]
                or Decimal("0.00")
        )

        breakdown_record = FiscalBreakdown.objects.filter(
            grant_id=grant,
            fiscal_year=fiscal_year,
        ).first()

        if grant.funding_sources == Form1.FundingSource.FEDERAL:
            federal = total_expenditure
            nonfederal = Decimal("0.00")
            difference = Decimal("0.00")
            allocation_needs_review = False
            reviewed_total = None

        elif grant.funding_sources == Form1.FundingSource.NONFEDERAL:
            federal = Decimal("0.00")
            nonfederal = total_expenditure
            difference = Decimal("0.00")
            allocation_needs_review = False
            reviewed_total = None

        elif grant.funding_sources == Form1.FundingSource.BOTH:
            federal = (
                breakdown_record.federal
                if breakdown_record
                else Decimal("0.00")
            )

            nonfederal = (
                    total_expenditure
                    - federal
            )

            difference = Decimal("0.00")

            reviewed_total = (
                breakdown_record.reviewed_total_allowed_expenditure
                if breakdown_record
                else None
            )

            allocation_needs_review = (
                    reviewed_total is None
                    or reviewed_total != total_expenditure
            )

        else:
            # Funding Sources still requires classification.
            # Preserve existing legacy allocation values but
            # do not treat them as authoritative.
            federal = (
                breakdown_record.federal
                if breakdown_record
                else Decimal("0.00")
            )

            nonfederal = (
                breakdown_record.nonfederal
                if breakdown_record
                else Decimal("0.00")
            )

            difference = (
                    total_expenditure
                    - federal
                    - nonfederal
            )

            reviewed_total = None
            allocation_needs_review = True

        breakdown["federal"] = federal
        breakdown["nonfederal"] = nonfederal
        breakdown["difference"] = difference
        breakdown["allocation_needs_review"] = (
            allocation_needs_review
        )
        breakdown["reviewed_total"] = reviewed_total

        total_expenditure_sum += total_expenditure
        total_federal_sum += federal
        total_nonfederal_sum += nonfederal
        total_difference += difference

    # Fetch fiscal year breakdown for Subsequent Adjustment
    subsequent_adjustments = (
        SubsequentAdjustment.objects
        .filter(grant_id=grant_id)
        .exclude(fiscal_year__isnull=True)
        .exclude(fiscal_year="")
    )

    subsequent_breakdown = (
        subsequent_adjustments
        .values("fiscal_year")
        .annotate(
            total_expenditure=Sum("net_expenditure")
        )
        .order_by("fiscal_year")
    )

    # Calculate totals for Subsequent Adjustment
    total_adjustment_sum = Decimal("0.00")
    total_federal_sub_sum = Decimal("0.00")
    total_nonfederal_sub_sum = Decimal("0.00")
    total_difference_sub = Decimal("0.00")

    for breakdown in subsequent_breakdown:
        fiscal_year = breakdown["fiscal_year"]

        total_adjustment = (
                breakdown["total_expenditure"]
                or Decimal("0.00")
        )

        sub_record = SubsequentFiscalBreakdown.objects.filter(
            grant_id=grant,
            fiscal_year=fiscal_year,
        ).first()

        if grant.funding_sources == Form1.FundingSource.FEDERAL:
            federal = total_adjustment
            nonfederal = Decimal("0.00")
            difference = Decimal("0.00")
            allocation_needs_review = False
            reviewed_total = None

        elif grant.funding_sources == Form1.FundingSource.NONFEDERAL:
            federal = Decimal("0.00")
            nonfederal = total_adjustment
            difference = Decimal("0.00")
            allocation_needs_review = False
            reviewed_total = None

        elif grant.funding_sources == Form1.FundingSource.BOTH:
            federal = (
                sub_record.federal
                if sub_record
                else Decimal("0.00")
            )

            nonfederal = (
                    total_adjustment
                    - federal
            )

            difference = Decimal("0.00")

            reviewed_total = (
                sub_record.reviewed_total_subsequent_adjustment
                if sub_record
                else None
            )

            allocation_needs_review = (
                    reviewed_total is None
                    or reviewed_total != total_adjustment
            )

        else:
            # Funding Sources still requires classification.
            # Preserve existing legacy allocation values but
            # do not treat them as authoritative.
            federal = (
                sub_record.federal
                if sub_record
                else Decimal("0.00")
            )

            nonfederal = (
                sub_record.nonfederal
                if sub_record
                else Decimal("0.00")
            )

            difference = (
                    total_adjustment
                    - federal
                    - nonfederal
            )

            reviewed_total = None
            allocation_needs_review = True

        breakdown["federal"] = federal
        breakdown["nonfederal"] = nonfederal
        breakdown["difference"] = difference
        breakdown["allocation_needs_review"] = (
            allocation_needs_review
        )
        breakdown["reviewed_total"] = reviewed_total

        total_adjustment_sum += total_adjustment
        total_federal_sub_sum += federal
        total_nonfederal_sub_sum += nonfederal
        total_difference_sub += difference

    program_income_records = {
        record.fiscal_year: record.amount
        for record in ProgramIncome.objects.filter(
            grant=grant
        )
    }

    program_income_rows = []
    total_program_income = Decimal("0.00")

    for fiscal_year in program_income_fiscal_years:
        amount = program_income_records.get(
            fiscal_year,
            Decimal("0.00"),
        )

        program_income_rows.append(
            {
                "fiscal_year": fiscal_year,
                "amount": amount,
            }
        )

        total_program_income += amount

    contract_total_allowed_expenditure = (
        total_expenditure_sum
        + total_adjustment_sum
    )

    if (
        grant.program_income_treatment
        == Form1.ProgramIncomeTreatment.ADDITIVE
    ):
        contract_balance = (
            grant.contract_amount
            + total_program_income
            - contract_total_allowed_expenditure
        )
    else:
        contract_balance = (
            grant.contract_amount
            - contract_total_allowed_expenditure
        )

    contract_balance_abs = abs(contract_balance)

    context = {
        'grant': grant,
        'form': form,
        "can_request_change": can_request_change,
        "active_change_request": active_change_request,
        "can_review_change_request": can_review_change_request,
        "can_edit_fiscal_data": can_edit_fiscal_data,
        'fiscal_breakdown': fiscal_breakdown,
        'total_expenditure_sum': total_expenditure_sum,
        'total_federal_sum': total_federal_sum,
        'total_nonfederal_sum': total_nonfederal_sum,
        'total_difference': total_difference,
        'subsequent_breakdown': subsequent_breakdown,
        'total_adjustment_sum': total_adjustment_sum,
        'total_federal_sub_sum': total_federal_sub_sum,
        'total_nonfederal_sub_sum': total_nonfederal_sub_sum,
        'total_difference_sub': total_difference_sub,
        "program_income_rows": program_income_rows,
        "total_program_income": total_program_income,
        "program_income_treatment_choices": (
            Form1.ProgramIncomeTreatment.choices
        ),
        "contract_total_allowed_expenditure": (
            contract_total_allowed_expenditure
        ),
        "contract_balance": contract_balance,
        "contract_balance_abs": contract_balance_abs,
        "fiscal_review": fiscal_review,
        "has_fiscal_exception_history": has_fiscal_exception_history,
    }

    return render(request, 'tracking/grant_detail.html', context)


@role_required(ROLE_EDITOR)
def create_grant_change_request(request, grant_id):
    grant = get_object_or_404(Form1, grant_id=grant_id)

    active_request = (
        ChangeRequest.objects
        .filter(
            grant_id=grant_id,
            status__in=CHANGE_REQUEST_BLOCKING_STATUSES,
        )
        .first()
    )

    if active_request:
        messages.error(
            request,
            (
                "This grant already has an active Basic Information "
                "Change Request."
            ),
        )
        return redirect(
            "grant_detail",
            grant_id=grant_id,
        )

    # Capture the authoritative values BEFORE binding/validating the
    # ModelForm. ModelForm validation may update its instance in memory.
    current_values = {
        field_name: serialize_change_request_value(
            getattr(grant, field_name)
        )
        for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    }

    snapshot_salt = "tracking.grant_basic_information_change"

    if request.method == "POST":
        submitted_snapshot_token = request.POST.get(
            "snapshot_token",
            "",
        )

        try:
            opened_values = signing.loads(
                submitted_snapshot_token,
                salt=snapshot_salt,
            )
        except signing.BadSignature:
            messages.error(
                request,
                (
                    "The Change Request form could not be verified. "
                    "Please reopen it and try again."
                ),
            )
            return redirect(
                "create_grant_change_request",
                grant_id=grant_id,
            )

        if opened_values != current_values:
            messages.error(
                request,
                (
                    "Grant Basic Information changed after this form "
                    "was opened. Please review the current values "
                    "before submitting a Change Request."
                ),
            )
            return redirect(
                "create_grant_change_request",
                grant_id=grant_id,
            )

        form = GrantBasicInformationChangeForm(
            request.POST,
            instance=grant,
        )

        if form.is_valid():
            proposed_values = {
                field_name: serialize_change_request_value(
                    form.cleaned_data.get(field_name)
                )
                for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
            }

            # Validate the proposed Award Code / GL date range against
            # authoritative grants. This same validation will be repeated
            # before final approval is applied.
            overlapping_grants = (
                Form1.objects
                .filter(
                    internal_award_code=form.cleaned_data[
                        "internal_award_code"
                    ],
                    internal_gl_end_date__gte=form.cleaned_data[
                        "internal_gl_start_date"
                    ],
                    internal_gl_start_date__lte=form.cleaned_data[
                        "internal_gl_end_date"
                    ],
                )
                .exclude(grant_id=grant_id)
                .order_by("grant_id")
            )

            if overlapping_grants.exists():
                conflicting_grant_ids = ", ".join(
                    overlapping_grants.values_list(
                        "grant_id",
                        flat=True,
                    )
                )

                form.add_error(
                    "internal_gl_end_date",
                    (
                        "The proposed Internal Award Code and GL date "
                        "range overlap with: "
                        f"{conflicting_grant_ids}."
                    ),
                )
            else:
                field_snapshots = []

                for field_name in (
                    GRANT_BASIC_INFORMATION_CHANGE_FIELDS
                ):
                    current_value = current_values[field_name]
                    proposed_value = proposed_values[field_name]

                    if proposed_value == current_value:
                        stored_proposed_value = None
                    else:
                        stored_proposed_value = proposed_value

                    field_snapshots.append(
                        {
                            "field_name": field_name,
                            "current_value": current_value,
                            "proposed_value": stored_proposed_value,
                        }
                    )

                has_changes = any(
                    snapshot["proposed_value"] is not None
                    for snapshot in field_snapshots
                )

                if not has_changes:
                    form.add_error(
                        None,
                        "No Basic Information changes were entered.",
                    )
                else:
                    try:
                        with transaction.atomic():
                            # Recheck inside the transaction. The database
                            # constraint is the final protection against a
                            # concurrent second active request.
                            if (
                                ChangeRequest.objects
                                .filter(
                                    grant_id=grant_id,
                                    status__in=CHANGE_REQUEST_BLOCKING_STATUSES,
                                )
                                .exists()
                            ):
                                messages.error(
                                    request,
                                    (
                                        "This grant already has an active "
                                        "Basic Information Change Request."
                                    ),
                                )
                                return redirect(
                                    "grant_detail",
                                    grant_id=grant_id,
                                )

                            change_request = ChangeRequest.objects.create(
                                grant_id=grant_id,
                                request_type=(
                                    ChangeRequest
                                    .RequestType
                                    .EDIT_GRANT
                                ),
                                status=ChangeRequest.Status.PENDING,
                                current_revision=1,
                                submitted_by=request.user,
                                submitted_at=timezone.now(),
                            )

                            ChangeRequestField.objects.bulk_create(
                                [
                                    ChangeRequestField(
                                        change_request=change_request,
                                        revision_no=1,
                                        field_name=snapshot[
                                            "field_name"
                                        ],
                                        current_value=snapshot[
                                            "current_value"
                                        ],
                                        proposed_value=snapshot[
                                            "proposed_value"
                                        ],
                                    )
                                    for snapshot in field_snapshots
                                ]
                            )

                    except IntegrityError:
                        messages.error(
                            request,
                            (
                                "Another active Change Request was "
                                "created for this grant. Please review "
                                "the existing request."
                            ),
                        )
                        return redirect(
                            "grant_detail",
                            grant_id=grant_id,
                        )

                    messages.success(
                        request,
                        (
                            "Basic Information Change Request "
                            "submitted for approval."
                        ),
                    )
                    return redirect(
                        "grant_detail",
                        grant_id=grant_id,
                    )

    else:
        form = GrantBasicInformationChangeForm(
            instance=grant,
        )

    snapshot_token = signing.dumps(
        current_values,
        salt=snapshot_salt,
    )

    return render(
        request,
        "tracking/grant_change_request_form.html",
        {
            "grant": grant,
            "form": form,
            "snapshot_token": snapshot_token,
            "current_values": current_values,
        },
    )


@role_required(ROLE_APPROVER)
def change_request_review(request, request_id):
    change_request = get_object_or_404(
        ChangeRequest,
        id=request_id,
    )

    grant = get_object_or_404(
        Form1,
        grant_id=change_request.grant_id,
    )

    revision_no = change_request.current_revision

    snapshots = {
        snapshot.field_name: snapshot
        for snapshot in (
            ChangeRequestField.objects
            .filter(
                change_request=change_request,
                revision_no=revision_no,
            )
        )
    }

    label_form = GrantBasicInformationChangeForm(
        instance=grant,
    )

    review_rows = []

    for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS:
        snapshot = snapshots.get(field_name)

        if not snapshot:
            continue

        changed = snapshot.proposed_value is not None

        review_rows.append(
            {
                "field_name": field_name,
                "label": label_form.fields[field_name].label,
                "current_value": (
                    format_change_request_display_value(
                        field_name,
                        snapshot.current_value,
                    )
                ),
                "proposed_value": (
                    format_change_request_display_value(
                        field_name,
                        snapshot.proposed_value,
                    )
                    if changed
                    else "No change"
                ),
                "changed": changed,
            }
        )

    approvals = (
        ChangeAction.objects
        .filter(
            change_request=change_request,
            revision_no=revision_no,
            action=ChangeAction.Action.APPROVE,
        )
        .select_related("acted_by")
        .order_by("acted_at", "id")
    )

    approval_count = approvals.count()

    user_has_approved = approvals.filter(
        acted_by=request.user,
    ).exists()

    can_approve = (
        change_request.status == ChangeRequest.Status.PENDING
        and change_request.submitted_by_id != request.user.id
        and not user_has_approved
        and approval_count == 0
    )

    if request.method == "POST":
        action = request.POST.get("action")

        if action != "approve":
            messages.error(
                request,
                "Invalid Change Request action.",
            )
            return redirect(
                "change_request_review",
                request_id=request_id,
            )

        try:
            with transaction.atomic():
                locked_request = (
                    ChangeRequest.objects
                    .select_for_update()
                    .get(id=request_id)
                )

                if (
                    locked_request.status
                    != ChangeRequest.Status.PENDING
                ):
                    messages.error(
                        request,
                        (
                            "This Change Request is no longer "
                            "pending approval."
                        ),
                    )
                    return redirect(
                        "change_request_review",
                        request_id=request_id,
                    )

                if locked_request.submitted_by_id == request.user.id:
                    messages.error(
                        request,
                        (
                            "You cannot approve a Change Request "
                            "that you submitted."
                        ),
                    )
                    return redirect(
                        "change_request_review",
                        request_id=request_id,
                    )

                existing_approval = (
                    ChangeAction.objects
                    .filter(
                        change_request=locked_request,
                        revision_no=locked_request.current_revision,
                        acted_by=request.user,
                        action=ChangeAction.Action.APPROVE,
                    )
                    .exists()
                )

                if existing_approval:
                    messages.info(
                        request,
                        (
                            "You have already approved this "
                            "revision."
                        ),
                    )
                    return redirect(
                        "change_request_review",
                        request_id=request_id,
                    )

                approval_count_before = (
                    ChangeAction.objects
                    .filter(
                        change_request=locked_request,
                        revision_no=locked_request.current_revision,
                        action=ChangeAction.Action.APPROVE,
                    )
                    .count()
                )

                # For this first workflow step, allow only approval #1.
                # Final approval/application will be implemented next.
                if approval_count_before >= 1:
                    messages.info(
                        request,
                        (
                            "This Change Request already has one "
                            "approval. Final approval processing "
                            "will be enabled in the next workflow "
                            "step."
                        ),
                    )
                    return redirect(
                        "change_request_review",
                        request_id=request_id,
                    )

                ChangeAction.objects.create(
                    change_request=locked_request,
                    revision_no=locked_request.current_revision,
                    acted_by=request.user,
                    action=ChangeAction.Action.APPROVE,
                    comment="",
                )

        except IntegrityError:
            messages.error(
                request,
                (
                    "The approval could not be recorded because "
                    "the request changed. Please review it again."
                ),
            )
            return redirect(
                "change_request_review",
                request_id=request_id,
            )

        messages.success(
            request,
            "Approval recorded. 1 of 2 approvals received.",
        )

        return redirect(
            "change_request_review",
            request_id=request_id,
        )

    return render(
        request,
        "tracking/change_request_review.html",
        {
            "change_request": change_request,
            "grant": grant,
            "review_rows": review_rows,
            "approvals": approvals,
            "approval_count": approval_count,
            "user_has_approved": user_has_approved,
            "can_approve": can_approve,
        },
    )


@login_required
def fiscal_exception_history(request, grant_id):
    grant = get_object_or_404(
        Form1,
        grant_id=grant_id,
    )

    fiscal_exception_history = (
        GrantFiscalExceptionReview.objects
        .filter(
            grant=grant,
            exception_type=(
                GrantFiscalExceptionReview
                .ExceptionType
                .NEGATIVE_CONTRACT_BALANCE
            ),
        )
        .select_related("accepted_by")
        .order_by("-accepted_at")
    )

    context = {
        "grant": grant,
        "fiscal_exception_history": fiscal_exception_history,
    }

    return render(
        request,
        "tracking/fiscal_exception_history.html",
        context,
    )


from django.db.models import Q
from django.contrib import messages

@role_required(ROLE_EDITOR)
def grant_create(request):
    if request.method == 'POST':
        # Create the form without validating it yet
        form = GrantForm(request.POST)

        # Debug: Print form data
        print(f"Form data received: {form.data}")

        # Generate the grant_id if it's a new grant
        last_grant = Form1.objects.order_by('grant_id').last()
        if last_grant and last_grant.grant_id.startswith('A'):
            last_id_num = int(last_grant.grant_id[1:])
            new_grant_id = f"A{last_id_num + 1:05d}"
        else:
            new_grant_id = "A00001"

        # Assign the generated grant_id to the form's data before validation
        form.data = form.data.copy()  # Make form data mutable
        form.data['grant_id'] = new_grant_id

        # Debug: Print the generated grant_id
        print(f"Generated grant_id: {new_grant_id}")

        # Now validate the form
        if form.is_valid():
            print("Form is valid.")


            # Extract form data for overlap check
            internal_award_code = form.cleaned_data['internal_award_code']
            internal_gl_start_date = form.cleaned_data['internal_gl_start_date']
            internal_gl_end_date = form.cleaned_data['internal_gl_end_date']

            # Check for existing grants with overlapping date ranges
            overlapping_grants = Form1.objects.filter(
                internal_award_code=internal_award_code,
                internal_gl_end_date__gte=internal_gl_start_date,
                internal_gl_start_date__lte=internal_gl_end_date,
            ).exclude(grant_id=new_grant_id)

            if overlapping_grants.exists():
                # Conflict found, list all conflicting grant IDs
                conflicting_grant_ids = ', '.join(grant.grant_id for grant in overlapping_grants)
                conflict_message = (
                    f"Conflict detected: Overlapping grants found with the following grant_ids: {conflicting_grant_ids}. "
                    "Please adjust the dates or check the existing records."
                )
                messages.error(request, conflict_message)
                print(conflict_message)

                # Return the form with an error message
                return render(request, 'tracking/grant_form.html', {'form': form})

            # Handle 'Add New' for dropdowns
            if form.cleaned_data['program_title'] == 'Add New':
                form.instance.program_title = form.cleaned_data['new_program_title']
            if form.cleaned_data['contracting_agency'] == 'Add New':
                form.instance.contracting_agency = form.cleaned_data['new_contracting_agency']
            if form.cleaned_data['federal_grantor'] == 'Add New':
                form.instance.federal_grantor = form.cleaned_data['new_federal_grantor']
            if form.cleaned_data['federal_aln'] == 'Add New':
                form.instance.federal_aln = form.cleaned_data['new_federal_aln']
            else:
                form.instance.federal_aln = form.cleaned_data['federal_aln']


            # Save the form and redirect
            form.save()
            return redirect('grant_list')
        else:
            # Debug: Print form errors
            print(f"Form is not valid. Errors: {form.errors}")

    else:
        form = GrantForm()

    return render(request, 'tracking/grant_form.html', {'form': form})




#def grant_edit(request, grant_id):
    #grant = get_object_or_404(Form1, grant_id=grant_id)
    #if request.method == 'POST':
        #form = GrantForm(request.POST, instance=grant)
        #if form.is_valid():
            # Check if the form has new values and update accordingly
            #new_program_title = form.cleaned_data.get('new_program_title')
            #new_contracting_agency = form.cleaned_data.get('new_contracting_agency')
            #new_federal_grantor = form.cleaned_data.get('new_federal_grantor')
            #new_federal_aln = form.cleaned_data.get('new_federal_aln')

            #if new_program_title:
                #form.instance.program_title = new_program_title
            #if new_contracting_agency:
                #form.instance.contracting_agency = new_contracting_agency
            #if new_federal_grantor:
                #form.instance.federal_grantor = new_federal_grantor
            #if new_federal_aln:
                #form.instance.federal_aln = new_federal_aln

            #form.save()
            #return redirect('grant_detail', grant_id=grant.grant_id)
    #else:
        #form = GrantForm(instance=grant)
    #return render(request, 'tracking/grant_form.html', {'form': form, 'edit': True})


@role_required(ROLE_ADMINISTRATOR)
def grant_delete(request, grant_id):
    grant = get_object_or_404(Form1, grant_id=grant_id)
    if request.method == 'POST':
        grant.delete()
        return redirect('grant_list')
    return render(request, 'tracking/grant_delete.html', {'grant': grant})



logger = logging.getLogger(__name__)

@role_required(ROLE_ACCOUNTANT)
def refresh_gl_expenditure(request):
    if request.method == 'POST' and request.FILES.get('gl_expenditure_file'):
        # Save the uploaded file to MEDIA_ROOT
        file = request.FILES['gl_expenditure_file']
        logger.info(f"File received: {file.name}")

        # Define the path to save the uploaded file
        media_path = os.path.join(settings.MEDIA_ROOT, 'uploads', f"{file.name}")
        with open(media_path, 'wb+') as destination:
            for chunk in file.chunks():
                destination.write(chunk)
        logger.info(f"File saved to: {media_path}")

        # Now proceed with the logic to read the Excel file and update the database
        try:
            # Load the Excel file
            df = pd.read_excel(media_path, usecols=["Effective Date", "Award Code", "Debit", "Credit"])
            logger.info(f"Excel data loaded: {df.head()}")

            # Ensure the "Effective" column is in datetime format
            df["Effective Date"] = pd.to_datetime(df["Effective Date"], format="%m/%d/%Y")

            # Ensure "Award Code" is a three-digit whole number (convert to string and pad with zeros if necessary)
            df["Award Code"] = df["Award Code"].apply(lambda x: f"{int(x):03d}")

            # Calculate net_expenditure
            df["net_expenditure"] = df["Credit"] - df["Debit"]

            # Define a function to calculate the fiscal year
            def calculate_fiscal_year(effective_date):
                year = effective_date.year
                if effective_date.month >= 10:  # If the month is October or later
                    fiscal_year = f"FY{year % 100:02d}-{(year + 1) % 100:02d}"
                else:
                    fiscal_year = f"FY{(year - 1) % 100:02d}-{year % 100:02d}"
                return fiscal_year

            # Add the fiscal_year column
            df["fiscal_year"] = df["Effective Date"].apply(calculate_fiscal_year)

            # Rename the columns to match the GLExpenditure model field names
            df.rename(columns={
                "Effective Date": "effective_date",
                "Award Code": "award_code",
                "Debit": "debit",
                "Credit": "credit"
            }, inplace=True)

            # Replace the GLExpenditure table contents atomically.
            # If import or grant assignment fails, the previous GL data
            # remains intact.
            with transaction.atomic():
                # Clear existing GLExpenditure records to prevent duplication
                GLExpenditure.objects.all().delete()
                logger.info("Cleared existing GLExpenditure records")

                # Insert data into GLExpenditure table
                for _, row in df.iterrows():
                    GLExpenditure.objects.create(
                        effective_date=row['effective_date'],
                        award_code=row['award_code'],
                        debit=Decimal(row['debit']),
                        credit=Decimal(row['credit']),
                        net_expenditure=Decimal(row['net_expenditure']),
                        fiscal_year=row['fiscal_year']
                    )

                # Assign GL transactions to grants using the shared
                # Award Code + Internal GL date-range matching service.
                assignment_result = rematch_gl_expenditures()

            logger.info(
                (
                    "GL grant assignment completed: "
                    "examined=%s, changed=%s, assigned=%s, unassigned=%s"
                ),
                assignment_result["examined"],
                assignment_result["changed"],
                assignment_result["assigned"],
                assignment_result["unassigned"],
            )

            logger.info("GL Expenditure data successfully refreshed.")
            return redirect('grant_list')

        except Exception as e:
            logger.error(f"An error occurred while processing the file: {str(e)}")
            return render(request, 'tracking/grant_list.html', {
                'message': f'An error occurred while processing the file: {str(e)}'
            })

    # If not a POST request or no file provided
    return render(request, 'tracking/grant_list.html', {
        'message': 'Please upload a valid Excel file.'
    })


@role_required(ROLE_ACCOUNTANT)
def refresh_subsequent_adjustment(request):
    if request.method == 'POST' and request.FILES.get('sub_adjustment_file'):
        # Save the uploaded file to MEDIA_ROOT
        file = request.FILES['sub_adjustment_file']
        logger.info(f"File received: {file.name}")

        # Define the path to save the uploaded file
        media_path = os.path.join(settings.MEDIA_ROOT, 'uploads', f"{file.name}")
        with open(media_path, 'wb+') as destination:
            for chunk in file.chunks():
                destination.write(chunk)
        logger.info(f"File saved to: {media_path}")

        try:
            # Load the Excel file
            df = pd.read_excel(media_path, usecols=["Effective Date", "Award Code", "Debit", "Credit", "Grant ID"])
            logger.info(f"Excel data loaded: {df.head()}")

            # Convert "Effective Date" to datetime format
            df["Effective Date"] = pd.to_datetime(df["Effective Date"], format="%m/%d/%Y")

            # Ensure "Award Code" is a three-digit whole number (pad with zeros if necessary)
            df["Award Code"] = df["Award Code"].apply(lambda x: f"{int(x):03d}")

            # Calculate net_expenditure
            df["net_expenditure"] = df["Credit"] - df["Debit"]

            # Define a function to calculate the fiscal year
            def calculate_fiscal_year(effective_date):
                year = effective_date.year
                if effective_date.month >= 10:  # October or later
                    return f"FY{year % 100:02d}-{(year + 1) % 100:02d}"
                return f"FY{(year - 1) % 100:02d}-{year % 100:02d}"

            # Add the fiscal_year column
            df["fiscal_year"] = df["Effective Date"].apply(calculate_fiscal_year)

            # Rename columns to match SubsequentAdjustment model
            df.rename(columns={
                "Effective Date": "effective_date",
                "Award Code": "award_code",
                "Debit": "debit",
                "Credit": "credit",
                "Grant ID": "grant_id"
            }, inplace=True)

            # Clear existing SubsequentAdjustment records
            SubsequentAdjustment.objects.all().delete()
            logger.info("Cleared existing SubsequentAdjustment records")

            # Insert data into SubsequentAdjustment table
            # Insert data into SubsequentAdjustment table
            for _, row in df.iterrows():
                grant_id_value = row['grant_id']

                # Check if grant_id is provided
                form1_instance = None
                if pd.notna(grant_id_value):  # Check if grant_id is not NaN
                    try:
                        form1_instance = Form1.objects.get(grant_id=grant_id_value)
                    except Form1.DoesNotExist:
                        logger.warning(f"Grant ID '{grant_id_value}' not found in Form1 table. Skipping this record.")
                        continue  # Skip this record if the grant_id is not found

                # Create the SubsequentAdjustment record
                try:
                    SubsequentAdjustment.objects.create(
                        effective_date=row['effective_date'],
                        award_code=row['award_code'],
                        debit=Decimal(row['debit']),
                        credit=Decimal(row['credit']),
                        net_expenditure=Decimal(row['net_expenditure']),
                        fiscal_year=row['fiscal_year'],
                        grant_id=form1_instance  # Use the Form1 instance or None
                    )
                except Exception as e:
                    logger.error(f"Error saving record: {str(e)}")

            logger.info("Subsequent Adjustment data successfully refreshed.")
            return redirect('grant_list')  # Redirect to refresh Grant List page

        except Exception as e:
            logger.error(f"An error occurred while processing the file: {str(e)}")
            return render(request, 'tracking/grant_list.html', {
                'message': f'An error occurred while processing the file: {str(e)}'
            })

    # If not a POST request or no file provided
    return render(request, 'tracking/grant_list.html', {
        'message': 'Please upload a valid Excel file for Subsequent Adjustment.'
    })


import csv
from django.http import HttpResponse
from django.db.models import Sum
from .models import Form1, GLExpenditure, SubsequentAdjustment, FiscalBreakdown, SubsequentFiscalBreakdown


def format_date_for_csv(value):
    if value is None:
        return ""

    return value.strftime("%Y-%m-%d")


@login_required
def download_data_csv(request):
    # Create HTTP response with CSV content type
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="grant_data.csv"'

    writer = csv.writer(response)

    # Write the CSV header
    writer.writerow([
        'Grant ID', 'Program Title', 'Contracting Agency', 'Contract Number',
        'Contract Start Date', 'Contract End Date', 'Contract Amount',
        'Federal Grantor', 'Federal ALN', 'Internal Award Code',
        'Internal GL Start Date', 'Internal GL End Date', 'Status',
        'Source', 'Fiscal Year', 'Total Expenditure', 'Federal',
        'Nonfederal', 'Difference'
    ])

    # Fetch all Form1 records
    form1_records = Form1.objects.all().order_by('grant_id')

    # Loop through each Form1 record
    for form in form1_records:
        grant_id = form.grant_id

        # Grouped data from GLExpenditure by fiscal_year
        gl_expenditures = GLExpenditure.objects.filter(grant_id=grant_id).values('fiscal_year').annotate(
            total_expenditure=Sum('net_expenditure')
        ).order_by('fiscal_year')

        if gl_expenditures.exists():
            for gl in gl_expenditures:
                fiscal_year = gl['fiscal_year']
                total_expenditure = gl['total_expenditure']

                # Fetch federal and nonfederal from FiscalBreakdown
                fiscal_record = FiscalBreakdown.objects.filter(
                    grant_id=grant_id, fiscal_year=fiscal_year
                ).first()

                federal = fiscal_record.federal if fiscal_record else 0
                nonfederal = fiscal_record.nonfederal if fiscal_record else 0
                difference = total_expenditure - federal - nonfederal

                writer.writerow([
                    grant_id, form.program_title, form.contracting_agency, form.contract_number,
                    format_date_for_csv(form.contract_start_date),
                    format_date_for_csv(form.contract_end_date),
                    form.contract_amount,
                    form.federal_grantor, form.federal_aln, form.internal_award_code,
                    format_date_for_csv(form.internal_gl_start_date),
                    format_date_for_csv(form.internal_gl_end_date),
                    form.status,
                    'GL Expenditure', fiscal_year, total_expenditure, federal,
                    nonfederal, difference
                ])
        else:
            # Write a row for the grant even if no GLExpenditure data exists
            writer.writerow([
                grant_id, form.program_title, form.contracting_agency, form.contract_number,
                format_date_for_csv(form.contract_start_date),
                format_date_for_csv(form.contract_end_date),
                form.contract_amount,
                form.federal_grantor, form.federal_aln, form.internal_award_code,
                format_date_for_csv(form.internal_gl_start_date),
                format_date_for_csv(form.internal_gl_end_date),
                form.status,
                'No Current Expenditure', '', '', '', '', ''
            ])

        # Grouped data from SubsequentAdjustment by fiscal_year
        subsequent_adjustments = SubsequentAdjustment.objects.filter(grant_id=grant_id).values('fiscal_year').annotate(
            total_adjustment=Sum('net_expenditure')
        ).order_by('fiscal_year')

        if subsequent_adjustments.exists():
            for sa in subsequent_adjustments:
                fiscal_year = sa['fiscal_year']
                total_adjustment = sa['total_adjustment']

                # Fetch federal and nonfederal from SubsequentFiscalBreakdown
                sub_fiscal_record = SubsequentFiscalBreakdown.objects.filter(
                    grant_id=grant_id, fiscal_year=fiscal_year
                ).first()

                federal_sub = sub_fiscal_record.federal if sub_fiscal_record else 0
                nonfederal_sub = sub_fiscal_record.nonfederal if sub_fiscal_record else 0
                difference_sub = total_adjustment - federal_sub - nonfederal_sub

                writer.writerow([
                    grant_id, form.program_title, form.contracting_agency, form.contract_number,
                    format_date_for_csv(form.contract_start_date),
                    format_date_for_csv(form.contract_end_date),
                    form.contract_amount,
                    form.federal_grantor, form.federal_aln, form.internal_award_code,
                    format_date_for_csv(form.internal_gl_start_date),
                    format_date_for_csv(form.internal_gl_end_date),
                    form.status,
                    'Subsequent Adjustment', fiscal_year, total_adjustment, federal_sub,
                    nonfederal_sub, difference_sub
                ])
        else:
            # Write a row for the grant even if no SubsequentAdjustment data exists
            writer.writerow([
                grant_id, form.program_title, form.contracting_agency, form.contract_number,
                format_date_for_csv(form.contract_start_date),
                format_date_for_csv(form.contract_end_date),
                form.contract_amount,
                form.federal_grantor, form.federal_aln, form.internal_award_code,
                format_date_for_csv(form.internal_gl_start_date),
                format_date_for_csv(form.internal_gl_end_date),
                form.status,
                'No Subsequent Adjustment', '', '', '', '', ''
            ])

    return response
