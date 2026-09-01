from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.db import transaction

from .forms import (
    GRANT_BASIC_INFORMATION_CHANGE_FIELDS,
    GrantBasicInformationChangeForm,
)
from .gl_assignment import rematch_gl_expenditures
from .models import (
    ChangeAction,
    ChangeRequest,
    ChangeRequestField,
    Form1,
)


GL_ASSIGNMENT_FIELDS = {
    "internal_award_code",
    "internal_gl_start_date",
    "internal_gl_end_date",
}


class ChangeRequestValidationError(Exception):
    """
    Raised when a submitted Change Request can no longer be safely applied.
    """


class ChangeRequestApprovalError(Exception):
    """
    Raised when a Change Request cannot receive the requested approval.
    """


class ChangeRequestReturnError(Exception):
    """
    Raised when a Change Request cannot be returned for revision.
    """


@dataclass(frozen=True)
class BasicInformationValidationResult:
    proposed_values: dict
    changed_fields: tuple
    gl_rematch_required: bool


@dataclass(frozen=True)
class StandaloneApprovalResult:
    change_request_id: int
    status: str
    approval_count: int
    changed_fields: tuple
    gl_rematch_result: object


@dataclass(frozen=True)
class StandaloneReturnResult:
    change_request_id: int
    status: str
    revision_no: int
    approval_count: int


def serialize_change_request_value(value):
    """
    Convert Basic Information values to stable text for audit storage.

    ChangeRequestField.proposed_value uses NULL to mean "unchanged",
    so an actual blank proposed value is stored as an empty string.
    """
    if value is None:
        return ""

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return format(value, "f")

    return str(value)


def validate_basic_information_change_request(change_request):
    """
    Revalidate the current revision of an existing-grant Basic Information
    Change Request without writing anything to the database.

    This verifies that:
      1. the revision has one snapshot for every expected Basic Information
         field;
      2. authoritative Form1 values still match the revision's stored
         current-value snapshot;
      3. the complete proposed Form1 state still passes the same form
         validation used during submission;
      4. the proposed Award Code / Internal GL date range does not overlap
         another authoritative grant.

    Returns a BasicInformationValidationResult when safe to continue.
    """
    revision_no = change_request.current_revision

    snapshots = list(
        ChangeRequestField.objects.filter(
            change_request=change_request,
            revision_no=revision_no,
        ).order_by("field_name")
    )

    expected_fields = set(GRANT_BASIC_INFORMATION_CHANGE_FIELDS)
    snapshot_fields = {
        snapshot.field_name
        for snapshot in snapshots
    }

    missing_fields = sorted(expected_fields - snapshot_fields)
    unexpected_fields = sorted(snapshot_fields - expected_fields)

    if missing_fields or unexpected_fields:
        details = []

        if missing_fields:
            details.append(
                "missing fields: " + ", ".join(missing_fields)
            )

        if unexpected_fields:
            details.append(
                "unexpected fields: " + ", ".join(unexpected_fields)
            )

        raise ChangeRequestValidationError(
            "The Change Request snapshot is incomplete or invalid ("
            + "; ".join(details)
            + ")."
        )

    snapshots_by_field = {
        snapshot.field_name: snapshot
        for snapshot in snapshots
    }

    grant = Form1.objects.get(
        grant_id=change_request.grant_id,
    )

    stale_fields = []

    for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS:
        authoritative_value = serialize_change_request_value(
            getattr(grant, field_name)
        )
        snapshot_current_value = (
            snapshots_by_field[field_name].current_value
        )

        if authoritative_value != snapshot_current_value:
            stale_fields.append(field_name)

    if stale_fields:
        raise ChangeRequestValidationError(
            "The authoritative grant no longer matches the current-value "
            "snapshot for this revision. Stale fields: "
            + ", ".join(stale_fields)
            + "."
        )

    proposed_form_data = {}

    for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS:
        snapshot = snapshots_by_field[field_name]

        if snapshot.proposed_value is None:
            proposed_form_data[field_name] = snapshot.current_value
        else:
            proposed_form_data[field_name] = snapshot.proposed_value

    # Use a fresh model instance because ModelForm validation may update its
    # instance in memory even when save() is never called.
    validation_grant = Form1.objects.get(
        grant_id=change_request.grant_id,
    )

    form = GrantBasicInformationChangeForm(
        data=proposed_form_data,
        instance=validation_grant,
    )

    if not form.is_valid():
        raise ChangeRequestValidationError(
            "The proposed Basic Information no longer passes validation: "
            + form.errors.as_text()
        )

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
        .exclude(grant_id=change_request.grant_id)
        .order_by("grant_id")
    )

    if overlapping_grants.exists():
        conflicting_grant_ids = ", ".join(
            overlapping_grants.values_list(
                "grant_id",
                flat=True,
            )
        )

        raise ChangeRequestValidationError(
            "The proposed Internal Award Code and GL date range overlap "
            f"with: {conflicting_grant_ids}."
        )

    changed_fields = tuple(
        field_name
        for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        if snapshots_by_field[field_name].proposed_value is not None
    )

    proposed_values = {
        field_name: form.cleaned_data.get(field_name)
        for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    }

    return BasicInformationValidationResult(
        proposed_values=proposed_values,
        changed_fields=changed_fields,
        gl_rematch_required=bool(
            GL_ASSIGNMENT_FIELDS.intersection(changed_fields)
        ),
    )


def _apply_validated_basic_information_values(
        *,
        grant,
        validation_result,
):
    """
    Apply an already-validated Basic Information proposal to Form1.

    This is intentionally a private low-level helper. It does not:
      - determine approval eligibility;
      - create ChangeAction records;
      - change ChangeRequest status; or
      - open its own transaction.

    The approval workflow must call it only after the request and grant
    have been locked and final validation has succeeded.
    """
    if not validation_result.changed_fields:
        raise ChangeRequestValidationError(
            "The Change Request contains no proposed Basic Information "
            "changes."
        )

    old_award_code = grant.internal_award_code

    for field_name in validation_result.changed_fields:
        setattr(
            grant,
            field_name,
            validation_result.proposed_values[field_name],
        )

    grant.save(
        update_fields=list(
            validation_result.changed_fields
        )
    )

    if not validation_result.gl_rematch_required:
        return None

    return rematch_gl_expenditures(
        award_codes={
            old_award_code,
            grant.internal_award_code,
        }
    )


def _get_revision_submitter_id(change_request):
    """
    Return the user responsible for submitting the current revision.

    Revision 1 uses ChangeRequest.submitted_by.
    Later revisions use the RESUBMIT action recorded for that revision.
    """
    revision_no = change_request.current_revision

    if revision_no == 1:
        if change_request.submitted_by_id is None:
            raise ChangeRequestApprovalError(
                "This Change Request has no recorded submitter."
            )

        return change_request.submitted_by_id

    resubmit_actions = list(
        ChangeAction.objects.filter(
            change_request=change_request,
            revision_no=revision_no,
            action=ChangeAction.Action.RESUBMIT,
        ).values_list(
            "acted_by_id",
            flat=True,
        )
    )

    if len(resubmit_actions) != 1:
        raise ChangeRequestApprovalError(
            "The current revision does not have exactly one recorded "
            "resubmission action."
        )

    return resubmit_actions[0]


def approve_standalone_change_request(
        *,
        change_request_id,
        approver,
):
    """
    Record approval #2 and atomically apply a standalone Basic Information
    Change Request.

    This service is intentionally limited to standalone EDIT_GRANT requests.
    Coordinated requests use a separate group-level application workflow.
    """
    with transaction.atomic():
        change_request = (
            ChangeRequest.objects
            .select_for_update()
            .get(pk=change_request_id)
        )

        if change_request.coordinated_change_id is not None:
            raise ChangeRequestApprovalError(
                "A coordinated Change Request cannot be finalized through "
                "the standalone approval workflow."
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise ChangeRequestApprovalError(
                "This approval service currently supports only existing-"
                "grant Basic Information Change Requests."
            )

        if change_request.status != ChangeRequest.Status.PENDING:
            raise ChangeRequestApprovalError(
                "This Change Request is no longer pending approval."
            )

        revision_no = change_request.current_revision

        revision_submitter_id = _get_revision_submitter_id(
            change_request
        )

        if revision_submitter_id == approver.id:
            raise ChangeRequestApprovalError(
                "You cannot approve a Change Request revision that you "
                "submitted."
            )

        existing_approval = ChangeAction.objects.filter(
            change_request=change_request,
            revision_no=revision_no,
            acted_by=approver,
            action=ChangeAction.Action.APPROVE,
        ).exists()

        if existing_approval:
            raise ChangeRequestApprovalError(
                "You have already approved this revision."
            )

        prior_approvals = list(
            ChangeAction.objects.filter(
                change_request=change_request,
                revision_no=revision_no,
                action=ChangeAction.Action.APPROVE,
            ).values_list(
                "acted_by_id",
                flat=True,
            )
        )

        if len(prior_approvals) != 1:
            raise ChangeRequestApprovalError(
                "Final approval requires exactly one existing approval "
                "for the current revision."
            )

        if prior_approvals[0] == revision_submitter_id:
            raise ChangeRequestApprovalError(
                "The existing approval was recorded by the submitter of this "
                "revision and cannot count toward final approval."
            )

        grant = (
            Form1.objects
            .select_for_update()
            .get(grant_id=change_request.grant_id)
        )

        validation_result = (
            validate_basic_information_change_request(
                change_request
            )
        )

        ChangeAction.objects.create(
            change_request=change_request,
            revision_no=revision_no,
            acted_by=approver,
            action=ChangeAction.Action.APPROVE,
            comment="",
        )

        gl_rematch_result = (
            _apply_validated_basic_information_values(
                grant=grant,
                validation_result=validation_result,
            )
        )

        change_request.status = ChangeRequest.Status.APPROVED
        change_request.save(
            update_fields=["status"]
        )

        return StandaloneApprovalResult(
            change_request_id=change_request.id,
            status=change_request.status,
            approval_count=2,
            changed_fields=validation_result.changed_fields,
            gl_rematch_result=gl_rematch_result,
        )


def return_standalone_change_request(
        *,
        change_request_id,
        approver,
        comment,
):
    """
    Return a standalone Basic Information Change Request for revision.

    The current revision and any approvals already recorded for it remain
    historical. No authoritative Form1 values are changed.
    """
    feedback = (comment or "").strip()

    if not feedback:
        raise ChangeRequestReturnError(
            "Return for Revision requires feedback."
        )

    if len(feedback) > 500:
        raise ChangeRequestReturnError(
            "Return for Revision feedback cannot exceed 500 characters."
        )

    with transaction.atomic():
        change_request = (
            ChangeRequest.objects
            .select_for_update()
            .get(pk=change_request_id)
        )

        if change_request.coordinated_change_id is not None:
            raise ChangeRequestReturnError(
                "A coordinated Change Request cannot be returned through "
                "the standalone workflow."
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise ChangeRequestReturnError(
                "This return service currently supports only existing-grant "
                "Basic Information Change Requests."
            )

        if change_request.status != ChangeRequest.Status.PENDING:
            raise ChangeRequestReturnError(
                "This Change Request is no longer pending review."
            )

        revision_no = change_request.current_revision

        try:
            revision_submitter_id = _get_revision_submitter_id(
                change_request
            )
        except ChangeRequestApprovalError as exc:
            raise ChangeRequestReturnError(
                str(exc)
            ) from exc

        if revision_submitter_id == approver.id:
            raise ChangeRequestReturnError(
                "You cannot return a Change Request revision that you "
                "submitted."
            )

        user_has_approved = ChangeAction.objects.filter(
            change_request=change_request,
            revision_no=revision_no,
            acted_by=approver,
            action=ChangeAction.Action.APPROVE,
        ).exists()

        if user_has_approved:
            raise ChangeRequestReturnError(
                "You have already approved this revision and cannot return "
                "the same revision for changes."
            )

        approval_count = ChangeAction.objects.filter(
            change_request=change_request,
            revision_no=revision_no,
            action=ChangeAction.Action.APPROVE,
        ).count()

        if approval_count >= 2:
            raise ChangeRequestReturnError(
                "A fully approved revision cannot be returned for changes."
            )

        ChangeAction.objects.create(
            change_request=change_request,
            revision_no=revision_no,
            acted_by=approver,
            action=ChangeAction.Action.RETURN,
            comment=feedback,
        )

        change_request.status = ChangeRequest.Status.RETURNED
        change_request.save(
            update_fields=["status"]
        )

        return StandaloneReturnResult(
            change_request_id=change_request.id,
            status=change_request.status,
            revision_no=revision_no,
            approval_count=approval_count,
        )
