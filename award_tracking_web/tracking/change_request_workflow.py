from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .forms import (
    GRANT_BASIC_INFORMATION_CHANGE_FIELDS,
    GrantBasicInformationChangeForm,
)
from .gl_assignment import rematch_gl_expenditures
from .models import (
    ChangeAction,
    ChangeRequest,
    ChangeRequestField,
    ChangeRequestIntegrityIssue,
    ChangeRequestIntegritySnapshotField,
    Form1,
)

from .permissions import (
    ROLE_ADMINISTRATOR,
    user_has_any_role,
)

GL_ASSIGNMENT_FIELDS = {
    "internal_award_code",
    "internal_gl_start_date",
    "internal_gl_end_date",
}


RESUBMISSION_BASELINE_FORMAL_REVISION = (
    "FORMAL_REVISION"
)

RESUBMISSION_BASELINE_INTEGRITY_DISPOSITION = (
    "INTEGRITY_DISPOSITION"
)


class ChangeRequestValidationError(Exception):
    """
    Raised when a submitted Change Request can no longer be safely applied.
    """


class ChangeRequestBaselineMismatchError(
        ChangeRequestValidationError
):
    """
    Raised when authoritative Form1 values no longer match the applicable
    recorded authoritative baseline.
    """

    def __init__(
            self,
            stale_fields,
            *,
            baseline_description=None,
    ):
        self.stale_fields = tuple(stale_fields)
        self.baseline_description = baseline_description

        if baseline_description is None:
            message = (
                "The authoritative grant no longer matches the current-value "
                "snapshot for this revision. Stale fields: "
                + ", ".join(self.stale_fields)
                + "."
            )
        else:
            message = (
                "The authoritative grant no longer matches "
                + baseline_description
                + ". Stale fields: "
                + ", ".join(self.stale_fields)
                + "."
            )

        super().__init__(message)


class ChangeRequestApprovalError(Exception):
    """
    Raised when a Change Request cannot receive the requested approval.
    """


class ChangeRequestReturnError(Exception):
    """
    Raised when a Change Request cannot be returned for revision.
    """


class ChangeRequestResubmitError(Exception):
    """
    Raised when a returned Change Request cannot be resubmitted.
    """


class ChangeRequestIntegrityIssueError(Exception):
    """
    Raised when a Change Request integrity incident cannot be recorded
    or used safely.
    """


class ChangeRequestIntegrityBlockedError(
        ChangeRequestValidationError
):
    """
    Raised after an integrity incident has been committed and normal
    Change Request workflow must remain blocked.
    """

    def __init__(
            self,
            integrity_issue_id,
            *,
            newly_detected,
    ):
        self.integrity_issue_id = integrity_issue_id
        self.newly_detected = newly_detected

        if newly_detected:
            message = (
                "An authoritative Basic Information integrity issue was "
                f"detected and recorded as Integrity Issue "
                f"#{integrity_issue_id}. Normal workflow actions are "
                "blocked until an Administrator resolves the issue."
            )
        else:
            message = (
                "This Change Request has an open authoritative Basic "
                f"Information integrity issue "
                f"(Integrity Issue #{integrity_issue_id}). Normal workflow "
                "actions are blocked until an Administrator resolves the "
                "issue."
            )

        super().__init__(message)


class ChangeRequestIntegrityDispositionError(Exception):
    """
    Raised when an integrity incident cannot be dispositioned safely.
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


@dataclass(frozen=True)
class StandaloneResubmitResult:
    change_request_id: int
    status: str
    previous_revision_no: int
    revision_no: int
    changed_fields: tuple


@dataclass(frozen=True)
class IntegrityIssueDetectionResult:
    integrity_issue_id: int
    created: bool
    revision_no: int


@dataclass(frozen=True)
class IntegrityIssueDispositionResult:
    integrity_issue_id: int
    change_request_id: int
    revision_no: int
    change_request_status: str
    classification: str
    checkpoint_field_count: int


@dataclass(frozen=True)
class ReturnedResubmissionBaselineResult:
    source: str
    baseline_values: dict
    integrity_issue_id: int | None


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


def get_open_change_request_integrity_issue(
        change_request,
        *,
        lock=False,
):
    """
    Return the currently OPEN integrity issue for a Change Request,
    if one exists.

    When lock=True, request a row lock for callers already operating inside
    a transaction.
    """
    issues = (
        ChangeRequestIntegrityIssue.objects
        .filter(
            change_request=change_request,
            status=ChangeRequestIntegrityIssue.Status.OPEN,
        )
    )

    if lock:
        issues = issues.select_for_update()

    return issues.first()


def get_integrity_snapshot_values(
        integrity_issue,
        *,
        stage,
        lock=False,
):
    """
    Return one complete integrity snapshot stage as a dictionary.

    A valid integrity snapshot stage must contain exactly one row for every
    protected Basic Information field and no unexpected fields.
    """
    valid_stages = {
        value
        for value, _label
        in ChangeRequestIntegritySnapshotField.Stage.choices
    }

    if stage not in valid_stages:
        raise ChangeRequestIntegrityIssueError(
            "Invalid integrity snapshot stage."
        )

    snapshots = (
        ChangeRequestIntegritySnapshotField.objects
        .filter(
            integrity_issue=integrity_issue,
            stage=stage,
        )
        .order_by("field_name")
    )

    if lock:
        snapshots = snapshots.select_for_update()

    snapshots = list(snapshots)

    expected_fields = set(
        GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    )

    snapshot_fields = {
        snapshot.field_name
        for snapshot in snapshots
    }

    missing_fields = sorted(
        expected_fields - snapshot_fields
    )

    unexpected_fields = sorted(
        snapshot_fields - expected_fields
    )

    if (
        len(snapshots) != len(expected_fields)
        or missing_fields
        or unexpected_fields
    ):
        details = []

        if missing_fields:
            details.append(
                "missing fields: "
                + ", ".join(missing_fields)
            )

        if unexpected_fields:
            details.append(
                "unexpected fields: "
                + ", ".join(unexpected_fields)
            )

        if not details:
            details.append(
                "the snapshot does not contain exactly one row "
                "for every protected field"
            )

        raise ChangeRequestIntegrityIssueError(
            "The integrity snapshot is incomplete or invalid ("
            + "; ".join(details)
            + ")."
        )

    return {
        snapshot.field_name: snapshot.authoritative_value
        for snapshot in snapshots
    }


def validate_returned_resubmission_baseline(
        change_request,
        *,
        grant=None,
        lock=False,
):
    """
    Validate the authoritative baseline that applies to a returned
    standalone Basic Information Change Request.

    Ordinary Return for Revision:
        validate against the current formal revision snapshot.

    Administrator Integrity Return:
        validate against the most recent closed integrity DISPOSITION
        checkpoint for the current formal revision.

    A DISPOSITION checkpoint takes precedence over the original formal
    revision baseline because it records the authoritative Form1 state
    accepted by the Administrator when resolving the integrity incident.
    """
    if change_request.status != ChangeRequest.Status.RETURNED:
        raise ChangeRequestValidationError(
            "The Change Request is not Returned for Revision."
        )

    existing_open_issue = (
        get_open_change_request_integrity_issue(
            change_request,
            lock=lock,
        )
    )

    if existing_open_issue is not None:
        raise ChangeRequestIntegrityBlockedError(
            existing_open_issue.id,
            newly_detected=False,
        )

    if grant is None:
        grants = Form1.objects.filter(
            grant_id=change_request.grant_id
        )

        if lock:
            grants = grants.select_for_update()

        grant = grants.get()

    elif grant.grant_id != change_request.grant_id:
        raise ChangeRequestValidationError(
            "The authoritative grant does not belong to this "
            "Change Request."
        )

    latest_closed_issue = (
        ChangeRequestIntegrityIssue.objects
        .filter(
            change_request=change_request,
            revision_no=change_request.current_revision,
            status=ChangeRequestIntegrityIssue.Status.CLOSED,
        )
        .order_by(
            "-closed_at",
            "-id",
        )
        .first()
    )

    if latest_closed_issue is None:
        grant, snapshots_by_field = (
            validate_basic_information_revision_baseline(
                change_request,
                grant=grant,
            )
        )

        baseline_values = {
            field_name: (
                snapshots_by_field[field_name].current_value
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

        return ReturnedResubmissionBaselineResult(
            source=(
                RESUBMISSION_BASELINE_FORMAL_REVISION
            ),
            baseline_values=baseline_values,
            integrity_issue_id=None,
        )

    try:
        baseline_values = get_integrity_snapshot_values(
            latest_closed_issue,
            stage=(
                ChangeRequestIntegritySnapshotField
                .Stage
                .DISPOSITION
            ),
            lock=lock,
        )

    except ChangeRequestIntegrityIssueError as exc:
        raise ChangeRequestValidationError(
            "The latest integrity disposition checkpoint is invalid: "
            + str(exc)
        ) from exc

    stale_fields = []

    for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS:
        authoritative_value = serialize_change_request_value(
            getattr(grant, field_name)
        )

        if (
            authoritative_value
            != baseline_values[field_name]
        ):
            stale_fields.append(field_name)

    if stale_fields:
        raise ChangeRequestBaselineMismatchError(
            stale_fields,
            baseline_description=(
                "the Administrator integrity disposition checkpoint "
                f"for Integrity Issue #{latest_closed_issue.id}"
            ),
        )

    return ReturnedResubmissionBaselineResult(
        source=(
            RESUBMISSION_BASELINE_INTEGRITY_DISPOSITION
        ),
        baseline_values=baseline_values,
        integrity_issue_id=latest_closed_issue.id,
    )


def detect_or_get_change_request_integrity_issue(
        *,
        change_request_id,
        detected_by,
        detected_during,
):
    """
    Record an OPEN Basic Information integrity incident when authoritative
    Form1 no longer matches the current formal revision baseline.

    The first detection captures all protected Basic Information fields as
    immutable DETECTION evidence.

    If an OPEN incident already exists for the Change Request, return that
    incident without creating duplicate issue or snapshot records.
    """
    valid_detection_stages = {
        value
        for value, _label
        in ChangeRequestIntegrityIssue.DetectedDuring.choices
    }

    if detected_during not in valid_detection_stages:
        raise ChangeRequestIntegrityIssueError(
            "Invalid integrity-incident detection stage."
        )

    with transaction.atomic():
        change_request = (
            ChangeRequest.objects
            .select_for_update()
            .get(pk=change_request_id)
        )

        existing_issue = (
            get_open_change_request_integrity_issue(
                change_request,
                lock=True,
            )
        )

        if existing_issue is not None:
            return IntegrityIssueDetectionResult(
                integrity_issue_id=existing_issue.id,
                created=False,
                revision_no=existing_issue.revision_no,
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise ChangeRequestIntegrityIssueError(
                "Integrity detection currently supports only existing-grant "
                "Basic Information Change Requests."
            )

        if change_request.current_revision < 1:
            raise ChangeRequestIntegrityIssueError(
                "The Change Request does not have a valid current revision."
            )

        grant = (
            Form1.objects
            .select_for_update()
            .get(grant_id=change_request.grant_id)
        )

        try:
            validate_basic_information_revision_baseline(
                change_request,
                grant=grant,
            )

        except ChangeRequestBaselineMismatchError:
            pass

        else:
            raise ChangeRequestIntegrityIssueError(
                "No authoritative Basic Information baseline mismatch "
                "currently exists for this Change Request."
            )

        detection_values = {
            field_name: serialize_change_request_value(
                getattr(grant, field_name)
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

        try:
            with transaction.atomic():
                integrity_issue = (
                    ChangeRequestIntegrityIssue.objects.create(
                        change_request=change_request,
                        revision_no=change_request.current_revision,
                        request_status_at_detection=(
                            change_request.status
                        ),
                        detected_during=detected_during,
                        detected_by=detected_by,
                    )
                )

                ChangeRequestIntegritySnapshotField.objects.bulk_create(
                    [
                        ChangeRequestIntegritySnapshotField(
                            integrity_issue=integrity_issue,
                            stage=(
                                ChangeRequestIntegritySnapshotField
                                .Stage
                                .DETECTION
                            ),
                            field_name=field_name,
                            authoritative_value=(
                                detection_values[field_name]
                            ),
                        )
                        for field_name
                        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
                    ]
                )

        except IntegrityError:
            # Final concurrency protection. If another transaction created
            # the OPEN incident first, reuse it instead of creating a
            # duplicate incident.
            existing_issue = (
                get_open_change_request_integrity_issue(
                    change_request,
                    lock=True,
                )
            )

            if existing_issue is None:
                raise

            return IntegrityIssueDetectionResult(
                integrity_issue_id=existing_issue.id,
                created=False,
                revision_no=existing_issue.revision_no,
            )

        return IntegrityIssueDetectionResult(
            integrity_issue_id=integrity_issue.id,
            created=True,
            revision_no=integrity_issue.revision_no,
        )


def resolve_change_request_integrity_issue(
        *,
        integrity_issue_id,
        administrator,
        classification,
        comment,
):
    """
    Resolve an OPEN Basic Information integrity incident.

    The Administrator classifies the incident, records a required
    explanation, captures all current authoritative Form1 values as the
    DISPOSITION checkpoint, closes the incident, and returns the Change
    Request to Editors.

    This service does not modify authoritative Form1 values and does not
    create an ordinary ChangeAction RETURN record.
    """
    if not user_has_any_role(
        administrator,
        ROLE_ADMINISTRATOR,
    ):
        raise ChangeRequestIntegrityDispositionError(
            "Only an Administrator can resolve an integrity incident."
        )

    resolution_comment = (comment or "").strip()

    if not resolution_comment:
        raise ChangeRequestIntegrityDispositionError(
            "Integrity resolution requires an explanation."
        )

    if len(resolution_comment) > 1000:
        raise ChangeRequestIntegrityDispositionError(
            "Integrity resolution explanation cannot exceed "
            "1000 characters."
        )

    valid_classifications = {
        value
        for value, _label
        in ChangeRequestIntegrityIssue.Classification.choices
    }

    if classification not in valid_classifications:
        raise ChangeRequestIntegrityDispositionError(
            "A valid integrity classification is required."
        )

    with transaction.atomic():
        integrity_issue = (
            ChangeRequestIntegrityIssue.objects
            .select_for_update()
            .get(pk=integrity_issue_id)
        )

        change_request = (
            ChangeRequest.objects
            .select_for_update()
            .get(pk=integrity_issue.change_request_id)
        )

        if (
            integrity_issue.status
            != ChangeRequestIntegrityIssue.Status.OPEN
        ):
            raise ChangeRequestIntegrityDispositionError(
                "This integrity incident is no longer open."
            )

        if change_request.coordinated_change_id is not None:
            raise ChangeRequestIntegrityDispositionError(
                "A coordinated Change Request cannot be resolved through "
                "the standalone integrity workflow."
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise ChangeRequestIntegrityDispositionError(
                "This integrity workflow currently supports only "
                "existing-grant Basic Information Change Requests."
            )

        if change_request.status not in {
            ChangeRequest.Status.PENDING,
            ChangeRequest.Status.RETURNED,
        }:
            raise ChangeRequestIntegrityDispositionError(
                "The Change Request is not in a state that can be "
                "returned to Editors through integrity resolution."
            )

        if (
            integrity_issue.revision_no
            != change_request.current_revision
        ):
            raise ChangeRequestIntegrityDispositionError(
                "The integrity incident does not belong to the current "
                "formal revision."
            )

        try:
            get_integrity_snapshot_values(
                integrity_issue,
                stage=(
                    ChangeRequestIntegritySnapshotField
                    .Stage
                    .DETECTION
                ),
                lock=True,
            )
        except ChangeRequestIntegrityIssueError as exc:
            raise ChangeRequestIntegrityDispositionError(
                str(exc)
            ) from exc

        disposition_exists = (
            ChangeRequestIntegritySnapshotField.objects
            .filter(
                integrity_issue=integrity_issue,
                stage=(
                    ChangeRequestIntegritySnapshotField
                    .Stage
                    .DISPOSITION
                ),
            )
            .exists()
        )

        if disposition_exists:
            raise ChangeRequestIntegrityDispositionError(
                "This integrity incident already contains a "
                "Disposition checkpoint."
            )

        grant = (
            Form1.objects
            .select_for_update()
            .get(grant_id=change_request.grant_id)
        )

        disposition_values = {
            field_name: serialize_change_request_value(
                getattr(grant, field_name)
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

        ChangeRequestIntegritySnapshotField.objects.bulk_create(
            [
                ChangeRequestIntegritySnapshotField(
                    integrity_issue=integrity_issue,
                    stage=(
                        ChangeRequestIntegritySnapshotField
                        .Stage
                        .DISPOSITION
                    ),
                    field_name=field_name,
                    authoritative_value=(
                        disposition_values[field_name]
                    ),
                )
                for field_name
                in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
            ]
        )

        integrity_issue.status = (
            ChangeRequestIntegrityIssue.Status.CLOSED
        )
        integrity_issue.classification = classification
        integrity_issue.closed_by = administrator
        integrity_issue.closed_at = timezone.now()
        integrity_issue.resolution_comment = (
            resolution_comment
        )
        integrity_issue.save(
            update_fields=[
                "status",
                "classification",
                "closed_by",
                "closed_at",
                "resolution_comment",
            ]
        )

        change_request.status = (
            ChangeRequest.Status.RETURNED
        )
        change_request.save(
            update_fields=["status"]
        )

        return IntegrityIssueDispositionResult(
            integrity_issue_id=integrity_issue.id,
            change_request_id=change_request.id,
            revision_no=integrity_issue.revision_no,
            change_request_status=change_request.status,
            classification=integrity_issue.classification,
            checkpoint_field_count=len(disposition_values),
        )


def validate_basic_information_revision_baseline(
        change_request,
        *,
        grant=None,
):
    """
    Validate the structural snapshot and authoritative baseline for the
    current Basic Information revision without writing to the database.

    This verifies that:
      1. the revision has one snapshot for every expected Basic Information
         field; and
      2. authoritative Form1 values still match the revision's stored
         current-value snapshot.

    The optional grant argument allows a caller that already locked Form1
    to validate that same authoritative record.

    Returns the authoritative grant and the snapshots keyed by field name.
    """
    revision_no = change_request.current_revision

    snapshots = list(
        ChangeRequestField.objects.filter(
            change_request=change_request,
            revision_no=revision_no,
        ).order_by("field_name")
    )

    expected_fields = set(
        GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    )

    snapshot_fields = {
        snapshot.field_name
        for snapshot in snapshots
    }

    missing_fields = sorted(
        expected_fields - snapshot_fields
    )

    unexpected_fields = sorted(
        snapshot_fields - expected_fields
    )

    if missing_fields or unexpected_fields:
        details = []

        if missing_fields:
            details.append(
                "missing fields: " + ", ".join(missing_fields)
            )

        if unexpected_fields:
            details.append(
                "unexpected fields: "
                + ", ".join(unexpected_fields)
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

    if grant is None:
        grant = Form1.objects.get(
            grant_id=change_request.grant_id,
        )
    elif grant.grant_id != change_request.grant_id:
        raise ChangeRequestValidationError(
            "The authoritative grant does not belong to this "
            "Change Request."
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
        raise ChangeRequestBaselineMismatchError(
            stale_fields
        )

    return grant, snapshots_by_field


def _validate_basic_information_proposed_form_data(
        *,
        change_request,
        grant,
        proposed_form_data,
):
    """
    Fully validate proposed Basic Information form data against the
    authoritative grant without writing to Form1.

    The caller is responsible for performing any required authoritative
    baseline check before calling this helper.
    """
    if grant.grant_id != change_request.grant_id:
        raise ChangeRequestValidationError(
            "The authoritative grant does not belong to this "
            "Change Request."
        )

    # Use a separate model instance because ModelForm validation may update
    # its instance in memory even when save() is never called.
    validation_grant = Form1.objects.get(
        grant_id=change_request.grant_id,
    )

    form = GrantBasicInformationChangeForm(
        data=proposed_form_data,
        instance=validation_grant,
    )

    if not form.is_valid():
        raise ChangeRequestValidationError(
            "The proposed Basic Information does not pass validation: "
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

    proposed_values = {
        field_name: form.cleaned_data.get(field_name)
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    }

    current_values = {
        field_name: serialize_change_request_value(
            getattr(grant, field_name)
        )
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    }

    changed_fields = tuple(
        field_name
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        if (
            serialize_change_request_value(
                proposed_values[field_name]
            )
            != current_values[field_name]
        )
    )

    if not changed_fields:
        raise ChangeRequestValidationError(
            "The Change Request contains no proposed Basic Information "
            "changes."
        )

    return BasicInformationValidationResult(
        proposed_values=proposed_values,
        changed_fields=changed_fields,
        gl_rematch_required=bool(
            GL_ASSIGNMENT_FIELDS.intersection(
                changed_fields
            )
        ),
    )


def validate_basic_information_change_request(
        change_request,
        *,
        grant=None,
):
    """
    Fully revalidate the current revision of an existing-grant Basic
    Information Change Request without writing anything to the database.

    This verifies that:
      1. the revision has one snapshot for every expected Basic Information
         field;
      2. authoritative Form1 values still match the revision's stored
         current-value snapshot;
      3. the complete proposed Form1 state still passes the same form
         validation used during submission; and
      4. the proposed Award Code / Internal GL date range does not overlap
         another authoritative grant.

    Returns a BasicInformationValidationResult when safe to continue.
    """
    grant, snapshots_by_field = (
        validate_basic_information_revision_baseline(
            change_request,
            grant=grant,
        )
    )

    proposed_form_data = {}

    for field_name in GRANT_BASIC_INFORMATION_CHANGE_FIELDS:
        snapshot = snapshots_by_field[field_name]

        if snapshot.proposed_value is None:
            proposed_form_data[field_name] = (
                snapshot.current_value
            )
        else:
            proposed_form_data[field_name] = (
                snapshot.proposed_value
            )

    return _validate_basic_information_proposed_form_data(
        change_request=change_request,
        grant=grant,
        proposed_form_data=proposed_form_data,
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


def get_revision_submitter_id(change_request):
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
    Record an approval for a standalone Basic Information Change Request.

    Approval #1 records the approval and leaves the request PENDING.

    Approval #2 revalidates and atomically applies the approved Basic
    Information proposal to Form1, then marks the request APPROVED.

    If an authoritative Basic Information baseline mismatch is detected,
    the integrity incident is committed first and the approval is then
    blocked outside the transaction.

    This service is intentionally limited to standalone EDIT_GRANT requests.
    Coordinated requests use a separate group-level approval workflow.
    """
    blocked_integrity_issue_id = None
    blocked_integrity_issue_created = False
    approval_result = None

    with transaction.atomic():
        change_request = (
            ChangeRequest.objects
            .select_for_update()
            .get(pk=change_request_id)
        )

        if change_request.coordinated_change_id is not None:
            raise ChangeRequestApprovalError(
                "A coordinated Change Request cannot be approved through "
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

        existing_integrity_issue = (
            get_open_change_request_integrity_issue(
                change_request,
                lock=True,
            )
        )

        if existing_integrity_issue is not None:
            blocked_integrity_issue_id = (
                existing_integrity_issue.id
            )
            blocked_integrity_issue_created = False

        else:
            revision_no = change_request.current_revision

            revision_submitter_id = get_revision_submitter_id(
                change_request
            )

            if revision_submitter_id == approver.id:
                raise ChangeRequestApprovalError(
                    "You cannot approve a Change Request revision that "
                    "you submitted."
                )

            existing_approval = (
                ChangeAction.objects
                .filter(
                    change_request=change_request,
                    revision_no=revision_no,
                    acted_by=approver,
                    action=ChangeAction.Action.APPROVE,
                )
                .exists()
            )

            if existing_approval:
                raise ChangeRequestApprovalError(
                    "You have already approved this revision."
                )

            prior_approvals = list(
                ChangeAction.objects
                .filter(
                    change_request=change_request,
                    revision_no=revision_no,
                    action=ChangeAction.Action.APPROVE,
                )
                .values_list(
                    "acted_by_id",
                    flat=True,
                )
            )

            if len(prior_approvals) > 1:
                raise ChangeRequestApprovalError(
                    "This revision already has enough approvals and cannot "
                    "receive another approval."
                )

            if (
                prior_approvals
                and prior_approvals[0] == revision_submitter_id
            ):
                raise ChangeRequestApprovalError(
                    "The existing approval was recorded by the submitter "
                    "of this revision and cannot count toward approval."
                )

            grant = (
                Form1.objects
                .select_for_update()
                .get(grant_id=change_request.grant_id)
            )

            try:
                validation_result = (
                    validate_basic_information_change_request(
                        change_request,
                        grant=grant,
                    )
                )

            except ChangeRequestBaselineMismatchError:
                detection_result = (
                    detect_or_get_change_request_integrity_issue(
                        change_request_id=change_request.id,
                        detected_by=approver,
                        detected_during=(
                            ChangeRequestIntegrityIssue
                            .DetectedDuring
                            .APPROVAL
                        ),
                    )
                )

                blocked_integrity_issue_id = (
                    detection_result.integrity_issue_id
                )
                blocked_integrity_issue_created = (
                    detection_result.created
                )

            else:
                ChangeAction.objects.create(
                    change_request=change_request,
                    revision_no=revision_no,
                    acted_by=approver,
                    action=ChangeAction.Action.APPROVE,
                    comment="",
                )

                if not prior_approvals:
                    approval_result = StandaloneApprovalResult(
                        change_request_id=change_request.id,
                        status=change_request.status,
                        approval_count=1,
                        changed_fields=(
                            validation_result.changed_fields
                        ),
                        gl_rematch_result=None,
                    )

                else:
                    gl_rematch_result = (
                        _apply_validated_basic_information_values(
                            grant=grant,
                            validation_result=validation_result,
                        )
                    )

                    change_request.status = (
                        ChangeRequest.Status.APPROVED
                    )
                    change_request.save(
                        update_fields=["status"]
                    )

                    approval_result = StandaloneApprovalResult(
                        change_request_id=change_request.id,
                        status=change_request.status,
                        approval_count=2,
                        changed_fields=(
                            validation_result.changed_fields
                        ),
                        gl_rematch_result=gl_rematch_result,
                    )

    if blocked_integrity_issue_id is not None:
        raise ChangeRequestIntegrityBlockedError(
            blocked_integrity_issue_id,
            newly_detected=(
                blocked_integrity_issue_created
            ),
        )

    return approval_result


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

    If an authoritative Basic Information baseline mismatch is detected,
    the integrity incident is committed first and the normal Return for
    Revision action is then blocked outside the transaction.
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

    blocked_integrity_issue_id = None
    blocked_integrity_issue_created = False
    return_result = None

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

        existing_integrity_issue = (
            get_open_change_request_integrity_issue(
                change_request,
                lock=True,
            )
        )

        if existing_integrity_issue is not None:
            blocked_integrity_issue_id = (
                existing_integrity_issue.id
            )
            blocked_integrity_issue_created = False

        else:
            revision_no = change_request.current_revision

            try:
                revision_submitter_id = get_revision_submitter_id(
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
                    "You have already approved this revision and cannot "
                    "return the same revision for changes."
                )

            approval_count = ChangeAction.objects.filter(
                change_request=change_request,
                revision_no=revision_no,
                action=ChangeAction.Action.APPROVE,
            ).count()

            if approval_count >= 2:
                raise ChangeRequestReturnError(
                    "A fully approved revision cannot be returned for "
                    "changes."
                )

            grant = (
                Form1.objects
                .select_for_update()
                .get(grant_id=change_request.grant_id)
            )

            try:
                validate_basic_information_revision_baseline(
                    change_request,
                    grant=grant,
                )

            except ChangeRequestBaselineMismatchError:
                detection_result = (
                    detect_or_get_change_request_integrity_issue(
                        change_request_id=change_request.id,
                        detected_by=approver,
                        detected_during=(
                            ChangeRequestIntegrityIssue
                            .DetectedDuring
                            .RETURN
                        ),
                    )
                )

                blocked_integrity_issue_id = (
                    detection_result.integrity_issue_id
                )
                blocked_integrity_issue_created = (
                    detection_result.created
                )

            else:
                ChangeAction.objects.create(
                    change_request=change_request,
                    revision_no=revision_no,
                    acted_by=approver,
                    action=ChangeAction.Action.RETURN,
                    comment=feedback,
                )

                change_request.status = (
                    ChangeRequest.Status.RETURNED
                )
                change_request.save(
                    update_fields=["status"]
                )

                return_result = StandaloneReturnResult(
                    change_request_id=change_request.id,
                    status=change_request.status,
                    revision_no=revision_no,
                    approval_count=approval_count,
                )

    if blocked_integrity_issue_id is not None:
        raise ChangeRequestIntegrityBlockedError(
            blocked_integrity_issue_id,
            newly_detected=(
                blocked_integrity_issue_created
            ),
        )

    return return_result


def resubmit_standalone_change_request(
        *,
        change_request_id,
        resubmitter,
        proposed_form_data,
        comment="",
):
    """
    Create the next formal revision of a returned standalone Basic
    Information Change Request.

    The previous revision remains immutable. The new revision receives a
    complete 14-field snapshot, a RESUBMIT action identifying the actual
    resubmitter, and begins PENDING with zero approvals.
    """
    resubmission_comment = (comment or "").strip()

    if len(resubmission_comment) > 500:
        raise ChangeRequestResubmitError(
            "Resubmission comments cannot exceed 500 characters."
        )

    with transaction.atomic():
        change_request = (
            ChangeRequest.objects
            .select_for_update()
            .get(pk=change_request_id)
        )

        if change_request.coordinated_change_id is not None:
            raise ChangeRequestResubmitError(
                "A coordinated Change Request cannot be resubmitted "
                "through the standalone workflow."
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise ChangeRequestResubmitError(
                "This resubmission service currently supports only "
                "existing-grant Basic Information Change Requests."
            )

        if change_request.status != ChangeRequest.Status.RETURNED:
            raise ChangeRequestResubmitError(
                "Only a Change Request that is Returned for Revision "
                "can be resubmitted."
            )

        previous_revision_no = (
            change_request.current_revision
        )

        return_count = (
            ChangeAction.objects
            .filter(
                change_request=change_request,
                revision_no=previous_revision_no,
                action=ChangeAction.Action.RETURN,
            )
            .count()
        )

        if return_count != 1:
            raise ChangeRequestResubmitError(
                "The returned revision does not have exactly one recorded "
                "Return for Revision action."
            )

        approval_count = (
            ChangeAction.objects
            .filter(
                change_request=change_request,
                revision_no=previous_revision_no,
                action=ChangeAction.Action.APPROVE,
            )
            .count()
        )

        if approval_count >= 2:
            raise ChangeRequestResubmitError(
                "A revision with two approvals cannot be resubmitted."
            )

        grant = (
            Form1.objects
            .select_for_update()
            .get(grant_id=change_request.grant_id)
        )

        validate_basic_information_revision_baseline(
            change_request,
            grant=grant,
        )

        validation_result = (
            _validate_basic_information_proposed_form_data(
                change_request=change_request,
                grant=grant,
                proposed_form_data=proposed_form_data,
            )
        )

        new_revision_no = previous_revision_no + 1

        if (
            ChangeRequestField.objects
            .filter(
                change_request=change_request,
                revision_no=new_revision_no,
            )
            .exists()
        ):
            raise ChangeRequestResubmitError(
                "The next revision already contains field snapshots."
            )

        if (
            ChangeAction.objects
            .filter(
                change_request=change_request,
                revision_no=new_revision_no,
            )
            .exists()
        ):
            raise ChangeRequestResubmitError(
                "The next revision already contains workflow actions."
            )

        current_values = {
            field_name: serialize_change_request_value(
                getattr(grant, field_name)
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

        proposed_values = {
            field_name: serialize_change_request_value(
                validation_result.proposed_values[field_name]
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

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
                ChangeRequestField(
                    change_request=change_request,
                    revision_no=new_revision_no,
                    field_name=field_name,
                    current_value=current_value,
                    proposed_value=stored_proposed_value,
                )
            )

        ChangeRequestField.objects.bulk_create(
            field_snapshots
        )

        ChangeAction.objects.create(
            change_request=change_request,
            revision_no=new_revision_no,
            acted_by=resubmitter,
            action=ChangeAction.Action.RESUBMIT,
            comment=resubmission_comment,
        )

        change_request.current_revision = (
            new_revision_no
        )
        change_request.status = (
            ChangeRequest.Status.PENDING
        )
        change_request.save(
            update_fields=[
                "current_revision",
                "status",
            ]
        )

        return StandaloneResubmitResult(
            change_request_id=change_request.id,
            status=change_request.status,
            previous_revision_no=previous_revision_no,
            revision_no=new_revision_no,
            changed_fields=validation_result.changed_fields,
        )
