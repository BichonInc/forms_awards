from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .forms import (
    GRANT_BASIC_INFORMATION_CHANGE_FIELDS,
    GrantBasicInformationChangeForm,
    BASIC_INFORMATION_DATE_ONLY_FIELDS,
)
from .gl_assignment import rematch_gl_expenditures
from .models import (
    CHANGE_REQUEST_BLOCKING_STATUSES,
    ChangeAction,
    ChangeRequest,
    ChangeRequestField,
    ChangeRequestIntegrityIssue,
    ChangeRequestIntegritySnapshotField,
    CoordinatedChange,
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


COORDINATED_CHILD_STATUS_BY_PARENT = {
    CoordinatedChange.Status.DRAFT: (
        ChangeRequest.Status.DRAFT
    ),
    CoordinatedChange.Status.PENDING: (
        ChangeRequest.Status.PENDING
    ),
    CoordinatedChange.Status.RETURNED: (
        ChangeRequest.Status.RETURNED
    ),
    CoordinatedChange.Status.APPLIED: (
        ChangeRequest.Status.APPROVED
    ),
    CoordinatedChange.Status.DENIED: (
        ChangeRequest.Status.DENIED
    ),
    CoordinatedChange.Status.CANCELLED: (
        ChangeRequest.Status.CANCELLED
    ),
    CoordinatedChange.Status.WITHDRAWN: (
        ChangeRequest.Status.WITHDRAWN
    ),
}


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


class ChangeRequestDenyError(Exception):
    """
    Raised when a Change Request cannot be denied.
    """


class ChangeRequestWithdrawError(Exception):
    """
    Raised when a Change Request cannot be withdrawn.
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


class CoordinatedChangeValidationError(
        ChangeRequestValidationError
):
    """
    Raised when a coordinated package is structurally invalid.
    """


class CoordinatedChangeBusinessValidationError(
        ChangeRequestValidationError
):
    """
    Raised when a structurally valid coordinated package fails
    coordinated business validation.
    """


class CoordinatedChangeApprovalError(
        ChangeRequestApprovalError
):
    """
    Raised when a coordinated package cannot receive the requested approval.
    """


class CoordinatedChangeIntegrityDispositionError(
        ChangeRequestIntegrityDispositionError
):
    """
    Raised when an integrity incident belonging to a coordinated package
    cannot receive the requested Administrator disposition.
    """


class CoordinatedChangeReturnError(
        ChangeRequestReturnError
):
    """
    Raised when a coordinated package cannot be returned for revision.
    """


class CoordinatedChangeDenyError(
        ChangeRequestDenyError
):
    """
    Raised when a coordinated package cannot be denied.
    """


class CoordinatedChangeResubmitError(
        ChangeRequestResubmitError
):
    """
    Raised when a returned coordinated package cannot be resubmitted.
    """


class CoordinatedChangeChildBaselineMismatchError(
        ChangeRequestBaselineMismatchError
):
    """
    Raised when one coordinated child no longer matches its
    authoritative Form1 baseline.
    """

    def __init__(
            self,
            change_request_id,
            stale_fields,
            *,
            baseline_description=None,
    ):
        self.change_request_id = change_request_id

        super().__init__(
            stale_fields,
            baseline_description=baseline_description,
        )

        original_message = str(self)

        self.args = (
            f"Child Change Request #{change_request_id}: "
            + original_message,
        )


class CoordinatedDraftConcurrencyError(
        CoordinatedChangeValidationError
):
    """
    Raised when a coordinated draft was saved after the caller loaded it.
    """

    def __init__(
            self,
            *,
            expected_version,
            actual_version,
    ):
        self.expected_version = expected_version
        self.actual_version = actual_version

        super().__init__(
            "The coordinated draft changed after this page was loaded. "
            f"Expected draft version {expected_version}, "
            f"but the current version is {actual_version}."
        )


class CoordinatedDraftAuthoritativeChangeError(
        CoordinatedChangeValidationError
):
    """
    Raised when authoritative Form1 changed beneath a saved draft.
    """

    def __init__(
            self,
            *,
            staleness_result,
    ):
        self.staleness_result = staleness_result
        self.baseline_differences = (
            staleness_result.baseline_differences
        )

        super().__init__(
            "Authoritative Basic Information changed after this "
            "coordinated draft was saved. Review the authoritative "
            "changes before saving the draft."
        )


class CoordinatedDraftReviewMismatchError(
        CoordinatedChangeValidationError
):
    """
    Raised when authoritative Form1 no longer matches the state that
    the editor reviewed before requesting a coordinated draft rebase.
    """

    def __init__(
            self,
            *,
            mismatches,
    ):
        self.mismatches = tuple(
            mismatches
        )

        super().__init__(
            "Authoritative Basic Information changed again after it "
            "was reviewed. Review the current authoritative values "
            "again before rebasing the coordinated draft."
        )


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
class StandaloneDenyResult:
    change_request_id: int
    status: str
    revision_no: int
    approval_count: int


@dataclass(frozen=True)
class StandaloneWithdrawResult:
    change_request_id: int
    status: str
    revision_no: int
    approval_count: int
    withdrawn_by_id: int


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


@dataclass(frozen=True)
class IntegrityIssueEvidenceResult:
    baseline_source: str
    baseline_integrity_issue_id: int | None
    baseline_values: dict
    detected_values: dict
    disposition_values: dict | None
    mismatched_fields: tuple


@dataclass(frozen=True)
class CoordinatedChangeStructureResult:
    coordinated_change_id: int
    status: str
    revision_no: int
    change_requests: tuple
    grant_ids: tuple


@dataclass(frozen=True)
class CoordinatedChildBasicInformationValidationResult:
    change_request_id: int
    grant_id: str
    current_award_code: str
    current_gl_start_date: datetime | None
    current_gl_end_date: datetime | None
    validation_result: BasicInformationValidationResult


@dataclass(frozen=True)
class CoordinatedBasicInformationValidationResult:
    coordinated_change_id: int
    revision_no: int
    children: tuple


@dataclass(frozen=True)
class CoordinatedFinalGLAssignment:
    grant_id: str
    change_request_id: int | None
    award_code: str
    gl_start_date: date
    gl_end_date: date


@dataclass(frozen=True)
class CoordinatedDraftBaselineDifference:
    change_request_id: int
    grant_id: str
    field_name: str
    saved_baseline_value: str | None
    authoritative_value: str | None
    draft_proposed_value: str | None


@dataclass(frozen=True)
class CoordinatedDraftStalenessResult:
    coordinated_change_id: int
    revision_no: int
    concurrency_version: int
    baseline_differences: tuple

    @property
    def has_authoritative_changes(self):
        return bool(
            self.baseline_differences
        )


@dataclass(frozen=True)
class CoordinatedDraftCreationResult:
    coordinated_change_id: int
    status: str
    revision_no: int
    concurrency_version: int
    change_request_ids: tuple
    grant_ids: tuple


@dataclass(frozen=True)
class CoordinatedDraftSaveResult:
    coordinated_change_id: int
    status: str
    revision_no: int
    previous_concurrency_version: int
    concurrency_version: int
    change_request_ids: tuple
    grant_ids: tuple
    added_grant_ids: tuple
    retained_grant_ids: tuple
    removed_grant_ids: tuple


@dataclass(frozen=True)
class CoordinatedDraftReviewedAuthoritativeMismatch:
    grant_id: str
    field_name: str
    reviewed_value: str
    authoritative_value: str


@dataclass(frozen=True)
class CoordinatedDraftRebaseResult:
    coordinated_change_id: int
    status: str
    revision_no: int
    previous_concurrency_version: int
    concurrency_version: int
    change_request_ids: tuple
    grant_ids: tuple
    rebased_differences: tuple


@dataclass(frozen=True)
class CoordinatedDraftSubmissionResult:
    coordinated_change_id: int
    status: str
    revision_no: int
    previous_concurrency_version: int
    concurrency_version: int
    change_request_ids: tuple
    grant_ids: tuple
    submitted_by_id: int
    submitted_at: datetime


@dataclass(frozen=True)
class CoordinatedApprovalResult:
    coordinated_change_id: int
    status: str
    revision_no: int
    approval_count: int
    change_request_ids: tuple
    grant_ids: tuple
    approver_id: int
    applied_at: datetime | None
    gl_rematch_result: object


@dataclass(frozen=True)
class CoordinatedIntegrityDispositionResult:
    coordinated_change_id: int
    status: str
    integrity_issue_id: int
    change_request_id: int
    revision_no: int
    classification: str
    checkpoint_field_count: int
    change_request_ids: tuple
    grant_ids: tuple


@dataclass(frozen=True)
class CoordinatedReturnResult:
    coordinated_change_id: int
    status: str
    revision_no: int
    approval_count: int
    change_request_ids: tuple
    grant_ids: tuple
    returned_by_id: int


@dataclass(frozen=True)
class CoordinatedDenyResult:
    coordinated_change_id: int
    status: str
    revision_no: int
    approval_count: int
    change_request_ids: tuple
    grant_ids: tuple
    denied_by_id: int


@dataclass(frozen=True)
class CoordinatedResubmitResult:
    coordinated_change_id: int
    status: str
    previous_revision_no: int
    revision_no: int
    change_request_ids: tuple
    grant_ids: tuple
    resubmitter_id: int


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


def _serialize_basic_information_comparison_value(
        field_name,
        value,
):
    """
    Serialize a Basic Information value for semantic change comparison.

    The four contract/GL date fields are business calendar dates even
    though Form1 currently stores them in DateTimeField columns.

    For those fields, timezone/time-of-day differences must not create a
    false workflow change when the calendar date itself is unchanged.

    Audit storage continues to use serialize_change_request_value(), so
    existing full datetime evidence is not rewritten or weakened.
    """
    if (
        field_name
        not in BASIC_INFORMATION_DATE_ONLY_FIELDS
    ):
        return serialize_change_request_value(
            value
        )

    if value is None:
        return ""

    if isinstance(value, datetime):
        return value.date().isoformat()

    if isinstance(value, date):
        return value.isoformat()

    value_text = str(value).strip()

    if not value_text:
        return ""

    normalized_text = (
        value_text[:-1] + "+00:00"
        if value_text.endswith("Z")
        else value_text
    )

    try:
        return (
            datetime
            .fromisoformat(normalized_text)
            .date()
            .isoformat()
        )

    except ValueError:
        try:
            return (
                date
                .fromisoformat(value_text)
                .isoformat()
            )

        except ValueError:
            # Validation will handle malformed date data.
            return value_text


def build_basic_information_field_snapshot_values(
        *,
        grant,
        proposed_values,
):
    """
    Build one complete set of serialized Basic Information field values.

    The returned values may be used for either mutable coordinated draft
    working rows or immutable formal revision snapshots.

    ChangeRequestField.proposed_value uses None to mean "unchanged".
    An actual proposed blank value remains the empty string.
    """
    missing_fields = [
        field_name
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        if field_name not in proposed_values
    ]

    if missing_fields:
        raise ChangeRequestValidationError(
            "The proposed Basic Information values are incomplete. "
            "Missing fields: "
            + ", ".join(missing_fields)
            + "."
        )

    field_snapshot_values = []

    for field_name in (
        GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    ):
        current_raw_value = getattr(
            grant,
            field_name,
        )

        proposed_raw_value = (
            proposed_values[
                field_name
            ]
        )

        current_value = (
            serialize_change_request_value(
                current_raw_value
            )
        )

        proposed_value = (
            serialize_change_request_value(
                proposed_raw_value
            )
        )

        current_comparison_value = (
            _serialize_basic_information_comparison_value(
                field_name,
                current_raw_value,
            )
        )

        proposed_comparison_value = (
            _serialize_basic_information_comparison_value(
                field_name,
                proposed_raw_value,
            )
        )

        if (
                proposed_comparison_value
                == current_comparison_value
        ):
            stored_proposed_value = None
        else:
            stored_proposed_value = proposed_value

        field_snapshot_values.append(
            {
                "field_name": field_name,
                "current_value": current_value,
                "proposed_value": (
                    stored_proposed_value
                ),
            }
        )

    return tuple(
        field_snapshot_values
    )


def get_basic_information_revision_snapshots(
        change_request,
        *,
        revision_no=None,
):
    """
    Return one formal Basic Information revision's snapshots keyed by
    field name.

    By default, use the Change Request's current revision. A specific
    historical revision may be requested for integrity-audit evidence.

    This validates snapshot structure without comparing the stored
    current-value baseline to authoritative Form1.
    """
    resolved_revision_no = (
        change_request.current_revision
        if revision_no is None
        else revision_no
    )

    if resolved_revision_no < 1:
        raise ChangeRequestValidationError(
            "The requested Change Request revision is invalid."
        )

    snapshots = list(
        ChangeRequestField.objects
        .filter(
            change_request=change_request,
            revision_no=resolved_revision_no,
        )
        .order_by("field_name")
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
                "the revision does not contain exactly one snapshot "
                "for every protected field"
            )

        raise ChangeRequestValidationError(
            "The Change Request snapshot is incomplete or invalid ("
            + "; ".join(details)
            + ")."
        )

    return {
        snapshot.field_name: snapshot
        for snapshot in snapshots
    }


def validate_coordinated_change_structure(
        coordinated_change,
        *,
        require_current_revision_snapshots=True,
        lock_children=False,
):
    """
    Validate the structural integrity of a coordinated package.

    This function validates package composition and synchronization.
    It does not validate proposed business values, GL overlap rules,
    or authoritative Form1 baseline integrity.

    A coordinated package must:
      1. contain at least two child Change Requests;
      2. contain unique grant IDs;
      3. keep every child on the package's current revision;
      4. keep child statuses synchronized with the package status;
      5. contain only EDIT_GRANT requests;
      6. reference an existing authoritative Form1 record for every
         child; and
      7. when required, contain one complete Basic Information
         snapshot for every child at the current package revision.

    lock_children=True is intended for callers already operating inside
    transaction.atomic(). It locks the child ChangeRequest rows before
    returning them.
    """
    if coordinated_change.pk is None:
        raise CoordinatedChangeValidationError(
            "The coordinated package has not been saved."
        )

    revision_no = coordinated_change.current_revision

    if revision_no < 1:
        raise CoordinatedChangeValidationError(
            "The coordinated package revision is invalid."
        )

    expected_child_status = (
        COORDINATED_CHILD_STATUS_BY_PARENT.get(
            coordinated_change.status
        )
    )

    if expected_child_status is None:
        raise CoordinatedChangeValidationError(
            "The coordinated package has an unsupported status."
        )

    child_query = (
        ChangeRequest.objects
        .filter(
            coordinated_change=coordinated_change
        )
        .order_by("id")
    )

    if lock_children:
        child_query = (
            child_query.select_for_update()
        )

    change_requests = tuple(
        child_query
    )

    if len(change_requests) < 2:
        raise CoordinatedChangeValidationError(
            "A coordinated package must contain at least "
            "two Change Requests."
        )

    grant_ids = tuple(
        change_request.grant_id
        for change_request
        in change_requests
    )

    duplicate_grant_ids = sorted(
        {
            grant_id
            for grant_id in grant_ids
            if grant_ids.count(grant_id) > 1
        }
    )

    if duplicate_grant_ids:
        raise CoordinatedChangeValidationError(
            "A coordinated package cannot contain the same "
            "grant more than once. Duplicate grant IDs: "
            + ", ".join(duplicate_grant_ids)
            + "."
        )

    revision_mismatches = [
        (
            change_request.id,
            change_request.current_revision,
        )
        for change_request
        in change_requests
        if (
            change_request.current_revision
            != revision_no
        )
    ]

    if revision_mismatches:
        details = ", ".join(
            (
                f"Request #{request_id} "
                f"is revision {child_revision}"
            )
            for request_id, child_revision
            in revision_mismatches
        )

        raise CoordinatedChangeValidationError(
            "The coordinated package and its child "
            "Change Requests are not on the same revision. "
            f"Package revision: {revision_no}; "
            + details
            + "."
        )

    status_mismatches = [
        (
            change_request.id,
            change_request.status,
        )
        for change_request
        in change_requests
        if (
            change_request.status
            != expected_child_status
        )
    ]

    if status_mismatches:
        details = ", ".join(
            (
                f"Request #{request_id} "
                f"has status {child_status}"
            )
            for request_id, child_status
            in status_mismatches
        )

        raise CoordinatedChangeValidationError(
            "The coordinated package and its child "
            "Change Requests have inconsistent statuses. "
            f"Package status {coordinated_change.status} "
            f"requires child status {expected_child_status}; "
            + details
            + "."
        )

    non_edit_request_ids = [
        change_request.id
        for change_request
        in change_requests
        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        )
    ]

    if non_edit_request_ids:
        raise CoordinatedChangeValidationError(
            "A coordinated package may contain only "
            "EDIT_GRANT Change Requests. Invalid Request IDs: "
            + ", ".join(
                str(request_id)
                for request_id
                in non_edit_request_ids
            )
            + "."
        )

    existing_grant_ids = set(
        Form1.objects
        .filter(
            grant_id__in=grant_ids
        )
        .values_list(
            "grant_id",
            flat=True,
        )
    )

    requests_without_grants = [
        change_request.id
        for change_request
        in change_requests
        if (
            change_request.grant_id
            not in existing_grant_ids
        )
    ]

    if requests_without_grants:
        raise CoordinatedChangeValidationError(
            "Every coordinated EDIT_GRANT request must "
            "reference an existing authoritative Form1 "
            "record. Invalid Request IDs: "
            + ", ".join(
                str(request_id)
                for request_id
                in requests_without_grants
            )
            + "."
        )

    if require_current_revision_snapshots:
        for change_request in change_requests:
            try:
                get_basic_information_revision_snapshots(
                    change_request,
                    revision_no=revision_no,
                )

            except ChangeRequestValidationError as exc:
                raise CoordinatedChangeValidationError(
                    f"Child Change Request "
                    f"#{change_request.id} has an invalid "
                    f"revision-{revision_no} snapshot: "
                    + str(exc)
                ) from exc

    return CoordinatedChangeStructureResult(
        coordinated_change_id=(
            coordinated_change.id
        ),
        status=coordinated_change.status,
        revision_no=revision_no,
        change_requests=change_requests,
        grant_ids=grant_ids,
    )


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


def get_change_request_integrity_evidence(
        integrity_issue,
):
    """
    Reconstruct the authoritative baseline that governed an integrity
    incident and compare it with the immutable DETECTION snapshot.

    Approval/Return incidents are governed by the formal revision
    snapshot.

    REVIEW/RESUBMIT incidents detected while the request was RETURNED
    use the latest preceding Administrator DISPOSITION checkpoint when
    one exists. Otherwise they use the formal revision snapshot.

    For a CLOSED incident, also return that incident's immutable
    DISPOSITION checkpoint.
    """
    change_request = integrity_issue.change_request

    detected_values = get_integrity_snapshot_values(
        integrity_issue,
        stage=(
            ChangeRequestIntegritySnapshotField
            .Stage
            .DETECTION
        ),
    )

    baseline_source = (
        RESUBMISSION_BASELINE_FORMAL_REVISION
    )

    baseline_integrity_issue_id = None

    preceding_disposition_issue = None

    if (
        integrity_issue.detected_during
        in {
            ChangeRequestIntegrityIssue
            .DetectedDuring
            .REVIEW,
            ChangeRequestIntegrityIssue
            .DetectedDuring
            .RESUBMIT,
        }
        and integrity_issue.request_status_at_detection
        == ChangeRequest.Status.RETURNED
    ):
        preceding_disposition_issue = (
            ChangeRequestIntegrityIssue.objects
            .filter(
                change_request=change_request,
                revision_no=integrity_issue.revision_no,
                status=(
                    ChangeRequestIntegrityIssue
                    .Status
                    .CLOSED
                ),
                closed_at__lte=integrity_issue.detected_at,
            )
            .exclude(pk=integrity_issue.pk)
            .order_by("-closed_at", "-id")
            .first()
        )

    if preceding_disposition_issue is not None:
        baseline_source = (
            RESUBMISSION_BASELINE_INTEGRITY_DISPOSITION
        )

        baseline_integrity_issue_id = (
            preceding_disposition_issue.id
        )

        baseline_values = get_integrity_snapshot_values(
            preceding_disposition_issue,
            stage=(
                ChangeRequestIntegritySnapshotField
                .Stage
                .DISPOSITION
            ),
        )

    else:
        revision_snapshots = (
            get_basic_information_revision_snapshots(
                change_request,
                revision_no=integrity_issue.revision_no,
            )
        )

        baseline_values = {
            field_name: revision_snapshots[
                field_name
            ].current_value
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

    mismatched_fields = tuple(
        field_name
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        if (
            baseline_values[field_name]
            != detected_values[field_name]
        )
    )

    disposition_values = None

    if (
        integrity_issue.status
        == ChangeRequestIntegrityIssue.Status.CLOSED
    ):
        disposition_values = get_integrity_snapshot_values(
            integrity_issue,
            stage=(
                ChangeRequestIntegritySnapshotField
                .Stage
                .DISPOSITION
            ),
        )

    return IntegrityIssueEvidenceResult(
        baseline_source=baseline_source,
        baseline_integrity_issue_id=(
            baseline_integrity_issue_id
        ),
        baseline_values=baseline_values,
        detected_values=detected_values,
        disposition_values=disposition_values,
        mismatched_fields=mismatched_fields,
    )


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
            if (
                    detected_during
                    in {
                        ChangeRequestIntegrityIssue
                        .DetectedDuring
                        .RESUBMIT,
                        ChangeRequestIntegrityIssue
                        .DetectedDuring
                        .REVIEW,
                    }
                    and change_request.status
                    == ChangeRequest.Status.RETURNED
            ):
                validate_returned_resubmission_baseline(
                    change_request,
                    grant=grant,
                    lock=True,
                )

            else:
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
        reviewed_authoritative_values,
):
    """
    Resolve an OPEN Basic Information integrity incident.

    The Administrator classifies the incident, records a required
    explanation, and confirms the authoritative Form1 values that were
    displayed for review.

    Inside the transaction, Form1 is locked and compared with those
    reviewed values. If any protected Basic Information value changed
    after the Administrator reviewed the page, disposition is refused.

    When the reviewed values still match authoritative Form1, all current
    protected values are captured as the immutable DISPOSITION checkpoint,
    the incident is closed, and the Change Request is returned to Editors.

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

    if not isinstance(
        reviewed_authoritative_values,
        dict,
    ):
        raise ChangeRequestIntegrityDispositionError(
            "The reviewed authoritative Basic Information snapshot "
            "is invalid."
        )

    expected_fields = set(
        GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    )

    reviewed_fields = set(
        reviewed_authoritative_values.keys()
    )

    missing_fields = sorted(
        expected_fields - reviewed_fields
    )

    unexpected_fields = sorted(
        reviewed_fields - expected_fields
    )

    if (
        missing_fields
        or unexpected_fields
        or len(reviewed_authoritative_values)
        != len(expected_fields)
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
                "the snapshot does not contain exactly one value "
                "for every protected field"
            )

        raise ChangeRequestIntegrityDispositionError(
            "The reviewed authoritative Basic Information snapshot "
            "is incomplete or invalid ("
            + "; ".join(details)
            + ")."
        )

    reviewed_values = {
        field_name: serialize_change_request_value(
            reviewed_authoritative_values[field_name]
        )
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    }

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

        stale_review_fields = tuple(
            field_name
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
            if (
                reviewed_values[field_name]
                != disposition_values[field_name]
            )
        )

        if stale_review_fields:
            raise ChangeRequestIntegrityDispositionError(
                "Authoritative Basic Information changed after the "
                "Administrator reviewed the integrity incident. "
                "Disposition was not recorded. Review the incident "
                "again before resolving it. Changed fields: "
                + ", ".join(stale_review_fields)
                + "."
            )

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


def resolve_coordinated_change_request_integrity_issue(
        *,
        integrity_issue_id,
        administrator,
        classification,
        comment,
        reviewed_authoritative_values,
):
    """
    Resolve one OPEN Basic Information integrity incident belonging to a
    coordinated package and return the entire package to Editors.

    The targeted incident receives its immutable DISPOSITION checkpoint
    and is closed using the Administrator's classification and explanation.

    The coordinated parent and every child Change Request then move together
    to RETURNED. No ordinary ChangeAction RETURN record is created because
    this is an Administrator integrity disposition, not an Approver Return
    for Revision.

    Other OPEN integrity incidents in the same package are not silently
    closed. They remain OPEN and continue to block later resubmission until
    they receive their own Administrator disposition.
    """
    if not user_has_any_role(
        administrator,
        ROLE_ADMINISTRATOR,
    ):
        raise CoordinatedChangeIntegrityDispositionError(
            "Only an Administrator can resolve an integrity incident."
        )

    resolution_comment = (comment or "").strip()

    if not resolution_comment:
        raise CoordinatedChangeIntegrityDispositionError(
            "Integrity resolution requires an explanation."
        )

    if len(resolution_comment) > 1000:
        raise CoordinatedChangeIntegrityDispositionError(
            "Integrity resolution explanation cannot exceed "
            "1000 characters."
        )

    valid_classifications = {
        value
        for value, _label
        in ChangeRequestIntegrityIssue.Classification.choices
    }

    if classification not in valid_classifications:
        raise CoordinatedChangeIntegrityDispositionError(
            "A valid integrity classification is required."
        )

    if not isinstance(
        reviewed_authoritative_values,
        dict,
    ):
        raise CoordinatedChangeIntegrityDispositionError(
            "The reviewed authoritative Basic Information snapshot "
            "is invalid."
        )

    expected_fields = set(
        GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    )

    reviewed_fields = set(
        reviewed_authoritative_values.keys()
    )

    missing_fields = sorted(
        expected_fields - reviewed_fields
    )

    unexpected_fields = sorted(
        reviewed_fields - expected_fields
    )

    if (
        missing_fields
        or unexpected_fields
        or len(reviewed_authoritative_values)
        != len(expected_fields)
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
                "the snapshot does not contain exactly one value "
                "for every protected field"
            )

        raise CoordinatedChangeIntegrityDispositionError(
            "The reviewed authoritative Basic Information snapshot "
            "is incomplete or invalid ("
            + "; ".join(details)
            + ")."
        )

    reviewed_values = {
        field_name: serialize_change_request_value(
            reviewed_authoritative_values[field_name]
        )
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    }

    # ---------------------------------------------------------
    # First locate the package without taking workflow locks.
    #
    # The authoritative transaction below locks the parent first
    # and then its children, matching the normal coordinated
    # workflow lock order.
    # ---------------------------------------------------------

    try:
        issue_locator = (
            ChangeRequestIntegrityIssue.objects
            .select_related(
                "change_request",
            )
            .get(
                pk=integrity_issue_id
            )
        )

    except ChangeRequestIntegrityIssue.DoesNotExist as exc:
        raise CoordinatedChangeIntegrityDispositionError(
            "The integrity incident does not exist."
        ) from exc

    coordinated_change_id = (
        issue_locator
        .change_request
        .coordinated_change_id
    )

    if coordinated_change_id is None:
        raise CoordinatedChangeIntegrityDispositionError(
            "This integrity incident belongs to a standalone "
            "Change Request and cannot be resolved through the "
            "coordinated integrity workflow."
        )

    with transaction.atomic():

        try:
            coordinated_change = (
                CoordinatedChange.objects
                .select_for_update()
                .get(
                    pk=coordinated_change_id
                )
            )

        except CoordinatedChange.DoesNotExist as exc:
            raise CoordinatedChangeIntegrityDispositionError(
                "The coordinated package does not exist."
            ) from exc

        if coordinated_change.status not in {
            CoordinatedChange.Status.PENDING,
            CoordinatedChange.Status.RETURNED,
        }:
            raise CoordinatedChangeIntegrityDispositionError(
                "The coordinated package is not in a state that "
                "can be returned to Editors through integrity "
                "resolution."
            )

        if coordinated_change.applied_at is not None:
            raise CoordinatedChangeIntegrityDispositionError(
                "An applied coordinated package cannot be returned "
                "through integrity resolution."
            )

        try:
            structure_result = (
                validate_coordinated_change_structure(
                    coordinated_change,
                    lock_children=True,
                )
            )

        except ChangeRequestValidationError as exc:
            raise CoordinatedChangeIntegrityDispositionError(
                str(exc)
            ) from exc

        change_requests = (
            structure_result.change_requests
        )

        revision_no = (
            structure_result.revision_no
        )

        change_requests_by_id = {
            change_request.id: change_request
            for change_request
            in change_requests
        }

        # -----------------------------------------------------
        # Lock the incident after the coordinated parent and
        # children so workflow locking remains package-first.
        # -----------------------------------------------------

        try:
            integrity_issue = (
                ChangeRequestIntegrityIssue.objects
                .select_for_update()
                .get(
                    pk=integrity_issue_id
                )
            )

        except ChangeRequestIntegrityIssue.DoesNotExist as exc:
            raise CoordinatedChangeIntegrityDispositionError(
                "The integrity incident no longer exists."
            ) from exc

        change_request = (
            change_requests_by_id.get(
                integrity_issue.change_request_id
            )
        )

        if change_request is None:
            raise CoordinatedChangeIntegrityDispositionError(
                "The integrity incident does not belong to a child "
                "Change Request in this coordinated package."
            )

        if (
            integrity_issue.status
            != ChangeRequestIntegrityIssue.Status.OPEN
        ):
            raise CoordinatedChangeIntegrityDispositionError(
                "This integrity incident is no longer open."
            )

        if (
            integrity_issue.revision_no
            != revision_no
        ):
            raise CoordinatedChangeIntegrityDispositionError(
                "The integrity incident does not belong to the "
                "current coordinated formal revision."
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise CoordinatedChangeIntegrityDispositionError(
                "This integrity workflow currently supports only "
                "existing-grant Basic Information Change Requests."
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
            raise CoordinatedChangeIntegrityDispositionError(
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
            raise CoordinatedChangeIntegrityDispositionError(
                "This integrity incident already contains a "
                "Disposition checkpoint."
            )

        try:
            grant = (
                Form1.objects
                .select_for_update()
                .get(
                    grant_id=change_request.grant_id
                )
            )

        except Form1.DoesNotExist as exc:
            raise CoordinatedChangeIntegrityDispositionError(
                "The authoritative grant for the integrity incident "
                "no longer exists."
            ) from exc

        disposition_values = {
            field_name: serialize_change_request_value(
                getattr(
                    grant,
                    field_name,
                )
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

        stale_review_fields = tuple(
            field_name
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
            if (
                reviewed_values[field_name]
                != disposition_values[field_name]
            )
        )

        if stale_review_fields:
            raise CoordinatedChangeIntegrityDispositionError(
                "Authoritative Basic Information changed after the "
                "Administrator reviewed the integrity incident. "
                "Disposition was not recorded. Review the incident "
                "again before resolving it. Changed fields: "
                + ", ".join(
                    stale_review_fields
                )
                + "."
            )

        # -----------------------------------------------------
        # Record the immutable Administrator disposition
        # checkpoint for the affected child only.
        # -----------------------------------------------------

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

        closed_at = timezone.now()

        integrity_issue.status = (
            ChangeRequestIntegrityIssue.Status.CLOSED
        )
        integrity_issue.classification = classification
        integrity_issue.closed_by = administrator
        integrity_issue.closed_at = closed_at
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

        # -----------------------------------------------------
        # Integrity disposition returns the WHOLE package.
        #
        # No ordinary RETURN ChangeAction is created. The closed
        # integrity incident is the audit evidence explaining the
        # package-level return.
        # -----------------------------------------------------

        for child in change_requests:
            child.status = (
                ChangeRequest.Status.RETURNED
            )

        ChangeRequest.objects.bulk_update(
            change_requests,
            ["status"],
        )

        coordinated_change.status = (
            CoordinatedChange.Status.RETURNED
        )

        coordinated_change.save(
            update_fields=[
                "status",
            ]
        )

        return CoordinatedIntegrityDispositionResult(
            coordinated_change_id=(
                coordinated_change.id
            ),
            status=coordinated_change.status,
            integrity_issue_id=(
                integrity_issue.id
            ),
            change_request_id=(
                change_request.id
            ),
            revision_no=revision_no,
            classification=(
                integrity_issue.classification
            ),
            checkpoint_field_count=(
                len(disposition_values)
            ),
            change_request_ids=tuple(
                child.id
                for child
                in change_requests
            ),
            grant_ids=tuple(
                child.grant_id
                for child
                in change_requests
            ),
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


def _validate_standalone_basic_information_overlap(
        *,
        change_request,
        proposed_values,
):
    """
    Validate one standalone edit's proposed Internal Award Code and
    Internal GL date range against authoritative Form1 records.

    Coordinated packages must not use this helper because their final
    GL assignments must be validated as one proposed package state.
    """
    overlapping_grants = (
        Form1.objects
        .filter(
            internal_award_code=proposed_values[
                "internal_award_code"
            ],
            internal_gl_end_date__gte=proposed_values[
                "internal_gl_start_date"
            ],
            internal_gl_start_date__lte=proposed_values[
                "internal_gl_end_date"
            ],
        )
        .exclude(
            grant_id=change_request.grant_id
        )
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


def _validate_basic_information_proposed_form_data(
        *,
        change_request,
        grant,
        proposed_form_data,
        require_changes=True,
):
    """
    Validate complete proposed Basic Information form data without
    writing to Form1 or performing GL-overlap validation.

    By default, at least one Basic Information field must change.
    Mutable coordinated draft callers may set require_changes=False
    while the draft is still being prepared.

    The caller is responsible for performing any required authoritative
    baseline check and the appropriate standalone or coordinated
    GL-assignment validation.
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

    proposed_values = {
        field_name: form.cleaned_data.get(field_name)
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
    }

    changed_fields = tuple(
        field_name
        for field_name
        in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        if (
            _serialize_basic_information_comparison_value(
                field_name,
                proposed_values[
                    field_name
                ],
            )
            !=
            _serialize_basic_information_comparison_value(
                field_name,
                getattr(
                    grant,
                    field_name,
                ),
            )
        )
    )

    if require_changes and not changed_fields:
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


def validate_coordinated_basic_information_proposals(
        coordinated_change,
        *,
        lock_children=False,
        lock_grants=False,
):
    """
    Validate every child proposal in a coordinated Basic Information
    package without performing package-wide GL-overlap validation.

    This function:
      - validates coordinated package structure;
      - validates every child's authoritative revision baseline;
      - reconstructs each child's complete proposed Form1 state;
      - applies the shared Basic Information form validation; and
      - requires every child to change at least one GL-assignment field.

    It does not determine whether the package's combined final GL
    assignments are valid. That is a separate coordinated validation
    step.

    lock_children=True and lock_grants=True are intended for callers
    already operating inside transaction.atomic().
    """
    structure_result = (
        validate_coordinated_change_structure(
            coordinated_change,
            require_current_revision_snapshots=True,
            lock_children=lock_children,
        )
    )

    grant_query = (
        Form1.objects
        .filter(
            grant_id__in=structure_result.grant_ids
        )
        .order_by("grant_id")
    )

    if lock_grants:
        grant_query = (
            grant_query.select_for_update()
        )

    grants_by_id = {
        grant.grant_id: grant
        for grant in grant_query
    }

    if (
        len(grants_by_id)
        != len(structure_result.grant_ids)
    ):
        raise CoordinatedChangeBusinessValidationError(
            "One or more authoritative grants required by the "
            "coordinated package are missing."
        )

    child_results = []

    for change_request in (
        structure_result.change_requests
    ):
        grant = grants_by_id[
            change_request.grant_id
        ]

        try:
            _, snapshots_by_field = (
                validate_basic_information_revision_baseline(
                    change_request,
                    grant=grant,
                )
            )

        except ChangeRequestBaselineMismatchError as exc:
            raise (
                CoordinatedChangeChildBaselineMismatchError(
                    change_request.id,
                    exc.stale_fields,
                    baseline_description=(
                        exc.baseline_description
                    ),
                )
            ) from exc

        proposed_form_data = {}

        for field_name in (
            GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        ):
            snapshot = snapshots_by_field[field_name]

            if snapshot.proposed_value is None:
                proposed_form_data[field_name] = (
                    snapshot.current_value
                )
            else:
                proposed_form_data[field_name] = (
                    snapshot.proposed_value
                )

        validation_result = (
            _validate_basic_information_proposed_form_data(
                change_request=change_request,
                grant=grant,
                proposed_form_data=proposed_form_data,
            )
        )

        if not validation_result.gl_rematch_required:
            raise CoordinatedChangeBusinessValidationError(
                f"Child Change Request "
                f"#{change_request.id} does not change "
                "Internal Award Code, Internal GL Start Date, "
                "or Internal GL End Date. Every coordinated "
                "child must contain a GL-assignment change."
            )

        child_results.append(
            CoordinatedChildBasicInformationValidationResult(
                change_request_id=change_request.id,
                grant_id=change_request.grant_id,
                current_award_code=(
                    grant.internal_award_code
                ),
                current_gl_start_date=(
                    grant.internal_gl_start_date
                ),
                current_gl_end_date=(
                    grant.internal_gl_end_date
                ),
                validation_result=validation_result,
            )
        )

    return CoordinatedBasicInformationValidationResult(
        coordinated_change_id=(
            coordinated_change.id
        ),
        revision_no=(
            structure_result.revision_no
        ),
        children=tuple(child_results),
    )


def _coordinated_gl_calendar_date(value):
    """
    Normalize a GL assignment date/datetime to its calendar date.

    Form1 stores the GL fields as DateTimeField values, while the business
    rule is date-based. Normalizing here prevents timezone offsets or DST
    transitions from changing whether two GL periods overlap or touch.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    raise CoordinatedChangeBusinessValidationError(
        "A coordinated GL assignment contains an unsupported date value."
    )


def _coordinated_child_gl_assignments(child_result):
    """
    Return the usable current and proposed GL assignments for one child.

    Current legacy assignments may be incomplete, so an incomplete
    current assignment does not create a relationship edge. The proposed
    assignment has already passed the shared Basic Information form
    validation.
    """
    proposed_values = (
        child_result.validation_result.proposed_values
    )

    raw_assignments = (
        (
            child_result.current_award_code,
            child_result.current_gl_start_date,
            child_result.current_gl_end_date,
        ),
        (
            proposed_values["internal_award_code"],
            proposed_values["internal_gl_start_date"],
            proposed_values["internal_gl_end_date"],
        ),
    )

    assignments = []

    for award_code, gl_start_date, gl_end_date in raw_assignments:
        normalized_award_code = str(
            award_code or ""
        ).strip()

        normalized_start_date = (
            _coordinated_gl_calendar_date(
                gl_start_date
            )
        )

        normalized_end_date = (
            _coordinated_gl_calendar_date(
                gl_end_date
            )
        )

        if (
            not normalized_award_code
            or normalized_start_date is None
            or normalized_end_date is None
        ):
            continue

        assignment = (
            normalized_award_code,
            normalized_start_date,
            normalized_end_date,
        )

        if assignment not in assignments:
            assignments.append(assignment)

    return tuple(assignments)


def _coordinated_gl_periods_overlap_or_touch(
        *,
        left_start_date,
        left_end_date,
        right_start_date,
        right_end_date,
):
    """
    Return True when two inclusive calendar-date GL periods overlap or
    are directly adjacent.

    Direct adjacency means one period begins on the calendar day
    immediately after the other period ends.
    """
    return (
        left_start_date
        <= right_end_date + timedelta(days=1)
        and right_start_date
        <= left_end_date + timedelta(days=1)
    )


def _coordinated_children_are_gl_related(
        left_child,
        right_child,
):
    """
    Return True when two coordinated children share a temporally related
    Award Code through any combination of current and proposed states.
    """
    left_assignments = (
        _coordinated_child_gl_assignments(
            left_child
        )
    )

    right_assignments = (
        _coordinated_child_gl_assignments(
            right_child
        )
    )

    for (
        left_award_code,
        left_start_date,
        left_end_date,
    ) in left_assignments:
        for (
            right_award_code,
            right_start_date,
            right_end_date,
        ) in right_assignments:

            if left_award_code != right_award_code:
                continue

            if _coordinated_gl_periods_overlap_or_touch(
                left_start_date=left_start_date,
                left_end_date=left_end_date,
                right_start_date=right_start_date,
                right_end_date=right_end_date,
            ):
                return True

    return False


def validate_coordinated_gl_relationship(
        validation_result,
):
    """
    Require all children to form one connected coordinated GL
    relationship.

    Two children are directly related when any current/proposed GL
    assignment from one child and any current/proposed assignment from
    the other child:

      1. use the same Internal Award Code; and
      2. have overlapping or directly adjacent calendar-date GL periods.

    Direct pairwise relationship is not required between every child.
    Chained relationships are valid as long as the whole package forms
    one connected component.
    """
    children = tuple(validation_result.children)

    if len(children) < 2:
        raise CoordinatedChangeBusinessValidationError(
            "A coordinated GL relationship requires at least two "
            "validated child Change Requests."
        )

    related_indexes = {
        index: set()
        for index in range(len(children))
    }

    for left_index in range(len(children)):
        for right_index in range(
                left_index + 1,
                len(children),
        ):
            if _coordinated_children_are_gl_related(
                children[left_index],
                children[right_index],
            ):
                related_indexes[left_index].add(
                    right_index
                )
                related_indexes[right_index].add(
                    left_index
                )

    unvisited_indexes = set(
        range(len(children))
    )

    components = []

    while unvisited_indexes:
        starting_index = min(
            unvisited_indexes
        )

        component = set()
        pending_indexes = [
            starting_index
        ]

        while pending_indexes:
            current_index = (
                pending_indexes.pop()
            )

            if current_index in component:
                continue

            component.add(current_index)
            unvisited_indexes.discard(
                current_index
            )

            pending_indexes.extend(
                related_indexes[current_index]
                - component
            )

        components.append(component)

    if len(components) > 1:
        component_descriptions = []

        for component in components:
            grant_ids = sorted(
                children[index].grant_id
                for index in component
            )

            component_descriptions.append(
                ", ".join(grant_ids)
            )

        raise CoordinatedChangeBusinessValidationError(
            "The coordinated package does not form one connected "
            "GL relationship. Related grant groups: "
            + "; ".join(component_descriptions)
            + "."
        )

    return validation_result


def _coordinated_final_gl_assignment_from_child(
        child_result,
):
    """
    Build one child's proposed final GL assignment.

    Final-state validation uses only the child's proposed Award Code and
    proposed GL dates. Its current assignment is intentionally excluded.
    """
    proposed_values = (
        child_result.validation_result.proposed_values
    )

    award_code = str(
        proposed_values["internal_award_code"]
        or ""
    ).strip()

    gl_start_date = (
        _coordinated_gl_calendar_date(
            proposed_values["internal_gl_start_date"]
        )
    )

    gl_end_date = (
        _coordinated_gl_calendar_date(
            proposed_values["internal_gl_end_date"]
        )
    )

    if (
        not award_code
        or gl_start_date is None
        or gl_end_date is None
    ):
        raise CoordinatedChangeBusinessValidationError(
            f"Child Change Request "
            f"#{child_result.change_request_id} does not contain "
            "a complete proposed final GL assignment."
        )

    return CoordinatedFinalGLAssignment(
        grant_id=child_result.grant_id,
        change_request_id=(
            child_result.change_request_id
        ),
        award_code=award_code,
        gl_start_date=gl_start_date,
        gl_end_date=gl_end_date,
    )


def _coordinated_final_gl_assignment_from_grant(
        grant,
):
    """
    Build an authoritative current GL assignment for an unaffected grant.

    Incomplete legacy assignments are ignored, matching the practical
    behavior of the existing standalone overlap query.
    """
    award_code = str(
        grant.internal_award_code or ""
    ).strip()

    gl_start_date = (
        _coordinated_gl_calendar_date(
            grant.internal_gl_start_date
        )
    )

    gl_end_date = (
        _coordinated_gl_calendar_date(
            grant.internal_gl_end_date
        )
    )

    if (
        not award_code
        or gl_start_date is None
        or gl_end_date is None
    ):
        return None

    return CoordinatedFinalGLAssignment(
        grant_id=grant.grant_id,
        change_request_id=None,
        award_code=award_code,
        gl_start_date=gl_start_date,
        gl_end_date=gl_end_date,
    )


def _coordinated_gl_periods_overlap(
        *,
        left_start_date,
        left_end_date,
        right_start_date,
        right_end_date,
):
    """
    Return True when two inclusive final GL periods actually overlap.

    Unlike relationship validation, directly adjacent periods do not
    conflict.
    """
    return (
        left_start_date <= right_end_date
        and right_start_date <= left_end_date
    )


def validate_coordinated_final_gl_state(
        validation_result,
        *,
        lock_outside_grants=False,
):
    """
    Validate the hypothetical post-package GL assignment state.

    Coordinated children are represented only by their proposed final
    assignments. Their current assignments are intentionally excluded.

    Unaffected Form1 grants remain represented by their current
    authoritative assignments.

    This rejects:
      - final overlap between two coordinated children; and
      - final overlap between a coordinated child and an unaffected
        authoritative grant.

    Existing conflicts solely between unaffected grants are outside this
    package and are not revalidated here.

    lock_outside_grants=True is intended for callers already operating
    inside transaction.atomic().
    """
    children = tuple(
        validation_result.children
    )

    if len(children) < 2:
        raise CoordinatedChangeBusinessValidationError(
            "Coordinated final-state validation requires at least two "
            "validated child Change Requests."
        )

    child_assignments = tuple(
        _coordinated_final_gl_assignment_from_child(
            child_result
        )
        for child_result in children
    )

    conflicts = []

    # ---------------------------------------------------------
    # Package child versus package child.
    # ---------------------------------------------------------

    for left_index in range(
            len(child_assignments)
    ):
        for right_index in range(
                left_index + 1,
                len(child_assignments),
        ):
            left_assignment = (
                child_assignments[left_index]
            )

            right_assignment = (
                child_assignments[right_index]
            )

            if (
                left_assignment.award_code
                != right_assignment.award_code
            ):
                continue

            if _coordinated_gl_periods_overlap(
                left_start_date=(
                    left_assignment.gl_start_date
                ),
                left_end_date=(
                    left_assignment.gl_end_date
                ),
                right_start_date=(
                    right_assignment.gl_start_date
                ),
                right_end_date=(
                    right_assignment.gl_end_date
                ),
            ):
                conflicts.append(
                    (
                        left_assignment.award_code,
                        left_assignment.grant_id,
                        right_assignment.grant_id,
                        "package",
                    )
                )

    package_grant_ids = {
        assignment.grant_id
        for assignment in child_assignments
    }

    affected_award_codes = {
        assignment.award_code
        for assignment in child_assignments
    }

    outside_grant_query = (
        Form1.objects
        .filter(
            internal_award_code__in=(
                affected_award_codes
            )
        )
        .exclude(
            grant_id__in=package_grant_ids
        )
        .order_by("grant_id")
    )

    if lock_outside_grants:
        outside_grant_query = (
            outside_grant_query.select_for_update()
        )

    # ---------------------------------------------------------
    # Package child versus unaffected authoritative Form1.
    # ---------------------------------------------------------

    for outside_grant in outside_grant_query:
        outside_assignment = (
            _coordinated_final_gl_assignment_from_grant(
                outside_grant
            )
        )

        if outside_assignment is None:
            continue

        for child_assignment in child_assignments:

            if (
                child_assignment.award_code
                != outside_assignment.award_code
            ):
                continue

            if _coordinated_gl_periods_overlap(
                left_start_date=(
                    child_assignment.gl_start_date
                ),
                left_end_date=(
                    child_assignment.gl_end_date
                ),
                right_start_date=(
                    outside_assignment.gl_start_date
                ),
                right_end_date=(
                    outside_assignment.gl_end_date
                ),
            ):
                conflicts.append(
                    (
                        child_assignment.award_code,
                        child_assignment.grant_id,
                        outside_assignment.grant_id,
                        "outside",
                    )
                )

    if conflicts:
        conflict_descriptions = []

        for (
            award_code,
            left_grant_id,
            right_grant_id,
            conflict_type,
        ) in sorted(conflicts):

            if conflict_type == "package":
                conflict_descriptions.append(
                    f"Award Code {award_code}: "
                    f"package grants {left_grant_id} and "
                    f"{right_grant_id} overlap"
                )

            else:
                conflict_descriptions.append(
                    f"Award Code {award_code}: "
                    f"package grant {left_grant_id} overlaps "
                    f"authoritative grant {right_grant_id}"
                )

        raise CoordinatedChangeBusinessValidationError(
            "The proposed final coordinated GL state contains "
            "overlapping assignments: "
            + "; ".join(conflict_descriptions)
            + "."
        )

    return validation_result


def validate_coordinated_basic_information_change(
        coordinated_change,
        *,
        lock_records=False,
):
    """
    Fully validate one coordinated existing-grant Basic Information
    package without writing to the database.

    Validation occurs in this order:

      1. reconstruct and validate every child's proposed final values;
      2. verify that all children form one coordinated GL relationship;
      3. verify that the hypothetical post-package GL state contains no
         overlapping assignments.

    When lock_records=True, package children, authoritative package
    grants, and relevant unaffected authoritative grants are locked for
    update. The caller must already be operating inside
    transaction.atomic().
    """
    validation_result = (
        validate_coordinated_basic_information_proposals(
            coordinated_change,
            lock_children=lock_records,
            lock_grants=lock_records,
        )
    )

    validate_coordinated_gl_relationship(
        validation_result
    )

    validate_coordinated_final_gl_state(
        validation_result,
        lock_outside_grants=lock_records,
    )

    return validation_result


def inspect_coordinated_draft_staleness(
        *,
        coordinated_change_id,
        expected_concurrency_version,
        lock_records=False,
):
    """
    Inspect whether a coordinated draft is stale.

    This performs two independent checks, in order:

      1. verify that the caller loaded the current saved draft version;
      2. compare each saved child baseline with authoritative Form1.

    A draft concurrency mismatch raises
    CoordinatedDraftConcurrencyError immediately.

    Authoritative Form1 differences are returned for review. They are
    not integrity incidents because the coordinated package is still
    DRAFT and its working baseline remains mutable.

    When lock_records=True, the package, child Change Requests, and
    authoritative Form1 rows are locked. The caller must already be
    inside transaction.atomic().
    """
    try:
        expected_version = int(
            expected_concurrency_version
        )
    except (TypeError, ValueError) as exc:
        raise CoordinatedChangeValidationError(
            "The expected coordinated draft version is invalid."
        ) from exc

    if expected_version < 1:
        raise CoordinatedChangeValidationError(
            "The expected coordinated draft version is invalid."
        )

    coordinated_change_query = (
        CoordinatedChange.objects
    )

    if lock_records:
        coordinated_change_query = (
            coordinated_change_query
            .select_for_update()
        )

    try:
        coordinated_change = (
            coordinated_change_query.get(
                pk=coordinated_change_id
            )
        )
    except CoordinatedChange.DoesNotExist as exc:
        raise CoordinatedChangeValidationError(
            "The coordinated draft does not exist."
        ) from exc

    if (
        coordinated_change.status
        != CoordinatedChange.Status.DRAFT
    ):
        raise CoordinatedChangeValidationError(
            "Draft staleness can be inspected only while the "
            "coordinated package is in Draft status."
        )

    # ---------------------------------------------------------
    # Check draft-to-draft concurrency FIRST.
    # ---------------------------------------------------------

    if (
        coordinated_change.concurrency_version
        != expected_version
    ):
        raise CoordinatedDraftConcurrencyError(
            expected_version=expected_version,
            actual_version=(
                coordinated_change.concurrency_version
            ),
        )

    revision_no = (
        coordinated_change.current_revision
    )

    child_query = (
        ChangeRequest.objects
        .filter(
            coordinated_change=coordinated_change
        )
        .order_by("id")
    )

    if lock_records:
        child_query = (
            child_query.select_for_update()
        )

    change_requests = tuple(
        child_query
    )

    # A working DRAFT may legitimately contain zero or one child.
    # Submit-time structural validation will require at least two.

    duplicate_grant_ids = sorted(
        {
            change_request.grant_id
            for change_request in change_requests
            if sum(
                1
                for other_request in change_requests
                if (
                    other_request.grant_id
                    == change_request.grant_id
                )
            ) > 1
        }
    )

    if duplicate_grant_ids:
        raise CoordinatedChangeValidationError(
            "A coordinated draft cannot contain the same grant "
            "more than once. Duplicate grant IDs: "
            + ", ".join(duplicate_grant_ids)
            + "."
        )

    for change_request in change_requests:

        if (
            change_request.status
            != ChangeRequest.Status.DRAFT
        ):
            raise CoordinatedChangeValidationError(
                f"Child Change Request #{change_request.id} "
                "is not in Draft status."
            )

        if (
            change_request.current_revision
            != revision_no
        ):
            raise CoordinatedChangeValidationError(
                f"Child Change Request #{change_request.id} "
                f"is revision {change_request.current_revision}, "
                f"but the coordinated draft is revision "
                f"{revision_no}."
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise CoordinatedChangeValidationError(
                f"Child Change Request #{change_request.id} "
                "is not an existing-grant edit request."
            )

    grant_ids = tuple(
        change_request.grant_id
        for change_request in change_requests
    )

    grant_query = (
        Form1.objects
        .filter(
            grant_id__in=grant_ids
        )
        .order_by("grant_id")
    )

    if lock_records:
        grant_query = (
            grant_query.select_for_update()
        )

    grants_by_id = {
        grant.grant_id: grant
        for grant in grant_query
    }

    missing_grant_ids = sorted(
        set(grant_ids)
        - set(grants_by_id)
    )

    if missing_grant_ids:
        raise CoordinatedChangeValidationError(
            "One or more coordinated draft grants no longer "
            "exist in authoritative Form1. Missing grant IDs: "
            + ", ".join(missing_grant_ids)
            + "."
        )

    baseline_differences = []

    # ---------------------------------------------------------
    # Compare the latest saved working baseline to Form1.
    # ---------------------------------------------------------

    for change_request in change_requests:

        try:
            snapshots_by_field = (
                get_basic_information_revision_snapshots(
                    change_request,
                    revision_no=revision_no,
                )
            )
        except ChangeRequestValidationError as exc:
            raise CoordinatedChangeValidationError(
                f"Child Change Request "
                f"#{change_request.id} has an invalid "
                f"revision-{revision_no} working snapshot: "
                + str(exc)
            ) from exc

        grant = grants_by_id[
            change_request.grant_id
        ]

        for field_name in (
            GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        ):
            snapshot = (
                snapshots_by_field[field_name]
            )

            authoritative_value = (
                serialize_change_request_value(
                    getattr(
                        grant,
                        field_name,
                    )
                )
            )

            saved_baseline_value = (
                snapshot.current_value
            )

            if (
                authoritative_value
                == saved_baseline_value
            ):
                continue

            if snapshot.proposed_value is None:
                draft_proposed_value = (
                    saved_baseline_value
                )
            else:
                draft_proposed_value = (
                    snapshot.proposed_value
                )

            baseline_differences.append(
                CoordinatedDraftBaselineDifference(
                    change_request_id=(
                        change_request.id
                    ),
                    grant_id=(
                        change_request.grant_id
                    ),
                    field_name=field_name,
                    saved_baseline_value=(
                        saved_baseline_value
                    ),
                    authoritative_value=(
                        authoritative_value
                    ),
                    draft_proposed_value=(
                        draft_proposed_value
                    ),
                )
            )

    return CoordinatedDraftStalenessResult(
        coordinated_change_id=(
            coordinated_change.id
        ),
        revision_no=revision_no,
        concurrency_version=(
            coordinated_change.concurrency_version
        ),
        baseline_differences=tuple(
            baseline_differences
        ),
    )


def create_coordinated_basic_information_draft(
        *,
        created_by,
        proposed_form_data_by_grant=None,
):
    """
    Create the initial mutable coordinated Basic Information draft.

    This is the first Save Draft operation. Because no coordinated
    package exists yet, there is no concurrency version to compare.

    A draft may contain zero, one, or multiple existing grants while it
    is being prepared. Each included child must contain complete,
    individually valid Basic Information form data, but it is not yet
    required to contain a change or a GL-assignment change.

    Package-level coordinated relationship and hypothetical final-state
    validation are intentionally deferred until submission.

    proposed_form_data_by_grant maps grant_id to one complete Basic
    Information form-data dictionary.
    """
    if proposed_form_data_by_grant is None:
        proposed_form_data_by_grant = {}

    try:
        proposal_items = tuple(
            proposed_form_data_by_grant.items()
        )
    except AttributeError as exc:
        raise CoordinatedChangeValidationError(
            "Coordinated draft proposals must be provided by grant ID."
        ) from exc

    normalized_proposals = []

    for grant_id, proposed_form_data in proposal_items:
        normalized_grant_id = str(
            grant_id
        ).strip()

        if not normalized_grant_id:
            raise CoordinatedChangeValidationError(
                "Every coordinated draft child must identify a grant."
            )

        normalized_proposals.append(
            (
                normalized_grant_id,
                proposed_form_data,
            )
        )

    grant_ids = tuple(
        grant_id
        for grant_id, _
        in normalized_proposals
    )

    duplicate_grant_ids = sorted(
        {
            grant_id
            for grant_id in grant_ids
            if grant_ids.count(grant_id) > 1
        }
    )

    if duplicate_grant_ids:
        raise CoordinatedChangeValidationError(
            "A coordinated draft cannot contain the same grant "
            "more than once. Duplicate grant IDs: "
            + ", ".join(duplicate_grant_ids)
            + "."
        )

    with transaction.atomic():

        # Lock authoritative grants in deterministic order before
        # creating any child requests or working snapshots.
        grant_query = (
            Form1.objects
            .filter(
                grant_id__in=grant_ids
            )
            .order_by("grant_id")
        )

        if grant_ids:
            grant_query = (
                grant_query.select_for_update()
            )

        grants = tuple(
            grant_query
        )

        grants_by_id = {
            grant.grant_id: grant
            for grant in grants
        }

        missing_grant_ids = sorted(
            set(grant_ids)
            - set(grants_by_id)
        )

        if missing_grant_ids:
            raise CoordinatedChangeValidationError(
                "Every coordinated draft child must reference "
                "an existing authoritative grant. Missing grant IDs: "
                + ", ".join(missing_grant_ids)
                + "."
            )

        # Existing active requests cannot be absorbed into a new
        # coordinated package.
        active_requests = tuple(
            ChangeRequest.objects
            .filter(
                grant_id__in=grant_ids,
                status__in=(
                    CHANGE_REQUEST_BLOCKING_STATUSES
                ),
            )
            .order_by(
                "grant_id",
                "id",
            )
        )

        if active_requests:
            active_descriptions = [
                (
                    f"{change_request.grant_id} "
                    f"(Request #{change_request.id})"
                )
                for change_request
                in active_requests
            ]

            raise CoordinatedChangeValidationError(
                "One or more grants already have an active "
                "Change Request: "
                + ", ".join(active_descriptions)
                + "."
            )

        coordinated_change = (
            CoordinatedChange.objects.create(
                status=(
                    CoordinatedChange.Status.DRAFT
                ),
                current_revision=1,
                concurrency_version=1,
                created_by=created_by,
            )
        )

        change_request_ids = []

        for (
            grant_id,
            proposed_form_data,
        ) in normalized_proposals:

            grant = grants_by_id[
                grant_id
            ]

            try:
                with transaction.atomic():
                    change_request = (
                        ChangeRequest.objects.create(
                            grant_id=grant_id,
                            request_type=(
                                ChangeRequest
                                .RequestType
                                .EDIT_GRANT
                            ),
                            status=(
                                ChangeRequest.Status.DRAFT
                            ),
                            coordinated_change=(
                                coordinated_change
                            ),
                            current_revision=1,
                        )
                    )

            except IntegrityError as exc:
                conflicting_request = (
                    ChangeRequest.objects
                    .filter(
                        grant_id=grant_id,
                        status__in=(
                            CHANGE_REQUEST_BLOCKING_STATUSES
                        ),
                    )
                    .order_by("id")
                    .first()
                )

                if conflicting_request is not None:
                    raise CoordinatedChangeValidationError(
                        f"Grant {grant_id} acquired another active "
                        "Change Request while this coordinated draft "
                        "was being saved. Conflicting Request #"
                        f"{conflicting_request.id}."
                    ) from exc

                raise

            try:
                validation_result = (
                    _validate_basic_information_proposed_form_data(
                        change_request=change_request,
                        grant=grant,
                        proposed_form_data=(
                            proposed_form_data
                        ),
                        require_changes=False,
                    )
                )

            except ChangeRequestValidationError as exc:
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} has invalid Basic Information "
                    "draft data: "
                    + str(exc)
                ) from exc

            field_snapshot_values = (
                build_basic_information_field_snapshot_values(
                    grant=grant,
                    proposed_values=(
                        validation_result.proposed_values
                    ),
                )
            )

            ChangeRequestField.objects.bulk_create(
                [
                    ChangeRequestField(
                        change_request=(
                            change_request
                        ),
                        revision_no=1,
                        field_name=(
                            snapshot[
                                "field_name"
                            ]
                        ),
                        current_value=(
                            snapshot[
                                "current_value"
                            ]
                        ),
                        proposed_value=(
                            snapshot[
                                "proposed_value"
                            ]
                        ),
                    )
                    for snapshot
                    in field_snapshot_values
                ]
            )

            change_request_ids.append(
                change_request.id
            )

        return CoordinatedDraftCreationResult(
            coordinated_change_id=(
                coordinated_change.id
            ),
            status=(
                coordinated_change.status
            ),
            revision_no=(
                coordinated_change.current_revision
            ),
            concurrency_version=(
                coordinated_change.concurrency_version
            ),
            change_request_ids=tuple(
                change_request_ids
            ),
            grant_ids=grant_ids,
        )


def save_coordinated_basic_information_draft(
        *,
        coordinated_change_id,
        expected_concurrency_version,
        proposed_form_data_by_grant=None,
):
    """
    Save a later version of an existing coordinated Basic Information
    draft.

    The package concurrency version is checked first. Existing saved
    child baselines are then compared with authoritative Form1 before
    any draft mutation occurs.

    If authoritative Form1 changed beneath an existing child, ordinary
    Save Draft is blocked so the editor can explicitly review and
    rebase that draft.

    proposed_form_data_by_grant represents the desired complete draft
    membership:
      - an existing grant that remains present is updated;
      - a new grant is added to the draft;
      - an existing grant that is omitted is removed from the request.

    Existing children's current_value baselines are never rewritten by
    ordinary Save Draft. Only their proposed_value working state may
    change.

    A successful save increments concurrency_version exactly once.
    """
    with transaction.atomic():

        # ---------------------------------------------------------
        # Concurrency MUST be checked first, followed by the
        # authoritative baseline check. This call locks the parent
        # first, then existing children and their Form1 records.
        # ---------------------------------------------------------

        staleness_result = (
            inspect_coordinated_draft_staleness(
                coordinated_change_id=(
                    coordinated_change_id
                ),
                expected_concurrency_version=(
                    expected_concurrency_version
                ),
                lock_records=True,
            )
        )

        if staleness_result.has_authoritative_changes:
            raise CoordinatedDraftAuthoritativeChangeError(
                staleness_result=staleness_result
            )

        coordinated_change = (
            CoordinatedChange.objects.get(
                pk=coordinated_change_id
            )
        )

        revision_no = (
            coordinated_change.current_revision
        )

        previous_concurrency_version = (
            coordinated_change.concurrency_version
        )

        # ---------------------------------------------------------
        # Normalize the desired complete draft membership only
        # AFTER concurrency and baseline staleness have passed.
        # ---------------------------------------------------------

        if proposed_form_data_by_grant is None:
            proposed_form_data_by_grant = {}

        try:
            proposal_items = tuple(
                proposed_form_data_by_grant.items()
            )
        except AttributeError as exc:
            raise CoordinatedChangeValidationError(
                "Coordinated draft proposals must be provided by "
                "grant ID."
            ) from exc

        normalized_proposals = []

        for grant_id, proposed_form_data in proposal_items:
            normalized_grant_id = str(
                grant_id
            ).strip()

            if not normalized_grant_id:
                raise CoordinatedChangeValidationError(
                    "Every coordinated draft child must identify "
                    "a grant."
                )

            normalized_proposals.append(
                (
                    normalized_grant_id,
                    proposed_form_data,
                )
            )

        desired_grant_ids = tuple(
            grant_id
            for grant_id, _
            in normalized_proposals
        )

        duplicate_grant_ids = sorted(
            {
                grant_id
                for grant_id in desired_grant_ids
                if desired_grant_ids.count(
                    grant_id
                ) > 1
            }
        )

        if duplicate_grant_ids:
            raise CoordinatedChangeValidationError(
                "A coordinated draft cannot contain the same grant "
                "more than once. Duplicate grant IDs: "
                + ", ".join(
                    duplicate_grant_ids
                )
                + "."
            )

        proposed_form_data_by_normalized_grant = {
            grant_id: proposed_form_data
            for grant_id, proposed_form_data
            in normalized_proposals
        }

        # ---------------------------------------------------------
        # Reload existing children. They are already protected by
        # the parent-first locking path above.
        # ---------------------------------------------------------

        existing_children = tuple(
            ChangeRequest.objects
            .select_for_update()
            .filter(
                coordinated_change=(
                    coordinated_change
                )
            )
            .order_by("id")
        )

        existing_children_by_grant = {
            change_request.grant_id: (
                change_request
            )
            for change_request
            in existing_children
        }

        existing_grant_ids = tuple(
            change_request.grant_id
            for change_request
            in existing_children
        )

        desired_grant_id_set = set(
            desired_grant_ids
        )

        existing_grant_id_set = set(
            existing_grant_ids
        )

        retained_grant_ids = tuple(
            grant_id
            for grant_id
            in desired_grant_ids
            if grant_id in existing_grant_id_set
        )

        added_grant_ids = tuple(
            grant_id
            for grant_id
            in desired_grant_ids
            if grant_id not in existing_grant_id_set
        )

        removed_grant_ids = tuple(
            grant_id
            for grant_id
            in existing_grant_ids
            if grant_id not in desired_grant_id_set
        )

        # ---------------------------------------------------------
        # Reject obvious active-request collisions before acquiring
        # locks on newly added grants.
        #
        # The database uniqueness constraint remains the final
        # defense against a request created after this precheck.
        # ---------------------------------------------------------

        active_requests = tuple(
            ChangeRequest.objects
            .filter(
                grant_id__in=added_grant_ids,
                status__in=(
                    CHANGE_REQUEST_BLOCKING_STATUSES
                ),
            )
            .order_by(
                "grant_id",
                "id",
            )
        )

        if active_requests:
            active_descriptions = [
                (
                    f"{change_request.grant_id} "
                    f"(Request #{change_request.id})"
                )
                for change_request
                in active_requests
            ]

            raise CoordinatedChangeValidationError(
                "One or more newly added grants already have an "
                "active Change Request: "
                + ", ".join(
                    active_descriptions
                )
                + "."
            )

        # ---------------------------------------------------------
        # Lock authoritative Form1 rows for newly added grants.
        # Existing grants were already locked by the staleness
        # inspection.
        # ---------------------------------------------------------

        added_grant_query = (
            Form1.objects
            .filter(
                grant_id__in=added_grant_ids
            )
            .order_by("grant_id")
        )

        if added_grant_ids:
            added_grant_query = (
                added_grant_query.select_for_update()
            )

        added_grants = tuple(
            added_grant_query
        )

        added_grants_by_id = {
            grant.grant_id: grant
            for grant in added_grants
        }

        missing_added_grant_ids = sorted(
            set(added_grant_ids)
            - set(added_grants_by_id)
        )

        if missing_added_grant_ids:
            raise CoordinatedChangeValidationError(
                "Every newly added coordinated draft child must "
                "reference an existing authoritative grant. Missing "
                "grant IDs: "
                + ", ".join(
                    missing_added_grant_ids
                )
                + "."
            )

        # ---------------------------------------------------------
        # Validate retained children's new working proposal state.
        #
        # IMPORTANT:
        # current_value remains the original saved draft baseline.
        # Ordinary Save Draft changes proposed_value only.
        # ---------------------------------------------------------

        retained_snapshot_updates = []

        for grant_id in retained_grant_ids:

            change_request = (
                existing_children_by_grant[
                    grant_id
                ]
            )

            try:
                grant = (
                    Form1.objects.get(
                        grant_id=grant_id
                    )
                )

                validation_result = (
                    _validate_basic_information_proposed_form_data(
                        change_request=change_request,
                        grant=grant,
                        proposed_form_data=(
                            proposed_form_data_by_normalized_grant[
                                grant_id
                            ]
                        ),
                        require_changes=False,
                    )
                )

            except ChangeRequestValidationError as exc:
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} has invalid Basic Information "
                    "draft data: "
                    + str(exc)
                ) from exc

            snapshots_by_field = (
                get_basic_information_revision_snapshots(
                    change_request,
                    revision_no=revision_no,
                )
            )

            for field_name in (
                GRANT_BASIC_INFORMATION_CHANGE_FIELDS
            ):
                snapshot = (
                    snapshots_by_field[
                        field_name
                    ]
                )

                proposed_value = (
                    serialize_change_request_value(
                        validation_result.proposed_values[
                            field_name
                        ]
                    )
                )

                if (
                    proposed_value
                    == snapshot.current_value
                ):
                    stored_proposed_value = None
                else:
                    stored_proposed_value = (
                        proposed_value
                    )

                if (
                    snapshot.proposed_value
                    != stored_proposed_value
                ):
                    snapshot.proposed_value = (
                        stored_proposed_value
                    )

                    retained_snapshot_updates.append(
                        snapshot
                    )

        # ---------------------------------------------------------
        # Validate newly added children's complete proposed states
        # before creating their persistent child records.
        # ---------------------------------------------------------

        added_validation_results = {}

        for grant_id in added_grant_ids:

            grant = added_grants_by_id[
                grant_id
            ]

            validation_change_request = (
                ChangeRequest(
                    grant_id=grant_id,
                    request_type=(
                        ChangeRequest
                        .RequestType
                        .EDIT_GRANT
                    ),
                    status=(
                        ChangeRequest.Status.DRAFT
                    ),
                    coordinated_change=(
                        coordinated_change
                    ),
                    current_revision=revision_no,
                )
            )

            try:
                validation_result = (
                    _validate_basic_information_proposed_form_data(
                        change_request=(
                            validation_change_request
                        ),
                        grant=grant,
                        proposed_form_data=(
                            proposed_form_data_by_normalized_grant[
                                grant_id
                            ]
                        ),
                        require_changes=False,
                    )
                )

            except ChangeRequestValidationError as exc:
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} has invalid Basic Information "
                    "draft data: "
                    + str(exc)
                ) from exc

            added_validation_results[
                grant_id
            ] = validation_result

        # ---------------------------------------------------------
        # Verify that every omitted child is genuinely removable
        # mutable draft state.
        #
        # Normal working field rows are intentionally removable.
        # Anything that looks like audit/history evidence blocks
        # physical removal.
        # ---------------------------------------------------------

        for grant_id in removed_grant_ids:

            change_request = (
                existing_children_by_grant[
                    grant_id
                ]
            )

            if (
                change_request.submitted_by_id is not None
                or change_request.submitted_at is not None
            ):
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} cannot be removed from the "
                    "request because its child Change Request has "
                    "submission history."
                )

            if (
                change_request.field_changes
                .exclude(
                    revision_no=revision_no
                )
                .exists()
            ):
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} cannot be removed from the "
                    "request because its child Change Request contains "
                    "historical revision snapshots."
                )

            if change_request.actions.exists():
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} cannot be removed from the "
                    "request because its child Change Request contains "
                    "workflow actions."
                )

            if change_request.notes.exists():
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} cannot be removed from the "
                    "request because its child Change Request contains "
                    "notes."
                )

            if (
                change_request.integrity_issues
                .exists()
            ):
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} cannot be removed from the "
                    "request because its child Change Request contains "
                    "integrity-audit history."
                )

            if change_request.documents.exists():
                raise CoordinatedChangeValidationError(
                    f"Grant {grant_id} cannot be removed from the "
                    "request because documents are attached to its "
                    "child Change Request."
                )

        # ---------------------------------------------------------
        # All validation has now succeeded. Begin draft mutation.
        # ---------------------------------------------------------

        if retained_snapshot_updates:
            ChangeRequestField.objects.bulk_update(
                retained_snapshot_updates,
                ["proposed_value"],
            )

        final_children_by_grant = {
            grant_id: (
                existing_children_by_grant[
                    grant_id
                ]
            )
            for grant_id
            in retained_grant_ids
        }

        # ---------------------------------------------------------
        # Add newly selected grants.
        # ---------------------------------------------------------

        for grant_id in added_grant_ids:

            grant = added_grants_by_id[
                grant_id
            ]

            validation_result = (
                added_validation_results[
                    grant_id
                ]
            )

            try:
                with transaction.atomic():
                    change_request = (
                        ChangeRequest.objects.create(
                            grant_id=grant_id,
                            request_type=(
                                ChangeRequest
                                .RequestType
                                .EDIT_GRANT
                            ),
                            status=(
                                ChangeRequest.Status.DRAFT
                            ),
                            coordinated_change=(
                                coordinated_change
                            ),
                            current_revision=revision_no,
                        )
                    )

            except IntegrityError as exc:
                conflicting_request = (
                    ChangeRequest.objects
                    .filter(
                        grant_id=grant_id,
                        status__in=(
                            CHANGE_REQUEST_BLOCKING_STATUSES
                        ),
                    )
                    .order_by("id")
                    .first()
                )

                if conflicting_request is not None:
                    raise CoordinatedChangeValidationError(
                        f"Grant {grant_id} acquired another active "
                        "Change Request while this coordinated draft "
                        "was being saved. Conflicting Request #"
                        f"{conflicting_request.id}."
                    ) from exc

                raise

            field_snapshot_values = (
                build_basic_information_field_snapshot_values(
                    grant=grant,
                    proposed_values=(
                        validation_result.proposed_values
                    ),
                )
            )

            ChangeRequestField.objects.bulk_create(
                [
                    ChangeRequestField(
                        change_request=(
                            change_request
                        ),
                        revision_no=revision_no,
                        field_name=(
                            snapshot[
                                "field_name"
                            ]
                        ),
                        current_value=(
                            snapshot[
                                "current_value"
                            ]
                        ),
                        proposed_value=(
                            snapshot[
                                "proposed_value"
                            ]
                        ),
                    )
                    for snapshot
                    in field_snapshot_values
                ]
            )

            final_children_by_grant[
                grant_id
            ] = change_request

        # ---------------------------------------------------------
        # Remove omitted grants FROM THE REQUEST.
        #
        # Never delete Form1. Only mutable coordinated-draft working
        # rows and their draft child ChangeRequest are removed.
        # ---------------------------------------------------------

        for grant_id in removed_grant_ids:

            change_request = (
                existing_children_by_grant[
                    grant_id
                ]
            )

            change_request.field_changes.filter(
                revision_no=revision_no
            ).delete()

            change_request.delete()

        # ---------------------------------------------------------
        # One successful Save Draft = one concurrency increment.
        # current_revision remains unchanged.
        # ---------------------------------------------------------

        coordinated_change.concurrency_version = (
            previous_concurrency_version + 1
        )

        coordinated_change.save(
            update_fields=[
                "concurrency_version",
            ]
        )

        final_change_request_ids = tuple(
            final_children_by_grant[
                grant_id
            ].id
            for grant_id
            in desired_grant_ids
        )

        return CoordinatedDraftSaveResult(
            coordinated_change_id=(
                coordinated_change.id
            ),
            status=(
                coordinated_change.status
            ),
            revision_no=revision_no,
            previous_concurrency_version=(
                previous_concurrency_version
            ),
            concurrency_version=(
                coordinated_change.concurrency_version
            ),
            change_request_ids=(
                final_change_request_ids
            ),
            grant_ids=desired_grant_ids,
            added_grant_ids=added_grant_ids,
            retained_grant_ids=(
                retained_grant_ids
            ),
            removed_grant_ids=(
                removed_grant_ids
            ),
        )


def rebase_coordinated_basic_information_draft(
        *,
        coordinated_change_id,
        expected_concurrency_version,
        reviewed_authoritative_values_by_grant,
):
    """
    Rebase an existing coordinated Basic Information draft onto
    authoritative Form1 values that the editor explicitly reviewed.

    This operation does not accept new draft proposals and does not
    create a new formal revision.

    Rules:

      1. coordinated draft concurrency is checked first;

      2. the existing draft baselines are compared with authoritative
         Form1 while the package, child requests, and authoritative
         grants are locked;

      3. the authoritative values must still exactly match the values
         the editor reviewed;

      4. stale current_value baselines are replaced with the reviewed
         authoritative values;

      5. an existing explicit proposed_value remains a proposal unless
         it now equals the new authoritative value, in which case it
         becomes None;

      6. a field with proposed_value=None remains None. The old stale
         baseline is never converted into a proposal;

      7. current_revision is unchanged;

      8. concurrency_version increments exactly once.

    Draft business/form validity is intentionally not revalidated here.
    An authoritative change may temporarily make an existing proposal
    inconsistent. After rebase, the editor can correct the proposal
    through ordinary Save Draft.
    """

    with transaction.atomic():

        # ---------------------------------------------------------
        # Concurrency must be checked first.
        #
        # The staleness inspection also locks the coordinated parent,
        # its existing children, and their authoritative Form1 rows.
        # ---------------------------------------------------------

        staleness_result = (
            inspect_coordinated_draft_staleness(
                coordinated_change_id=(
                    coordinated_change_id
                ),
                expected_concurrency_version=(
                    expected_concurrency_version
                ),
                lock_records=True,
            )
        )

        if not staleness_result.has_authoritative_changes:
            raise CoordinatedChangeValidationError(
                "This coordinated draft does not have authoritative "
                "Basic Information changes that require review and "
                "rebase."
            )

        coordinated_change = (
            CoordinatedChange.objects.get(
                pk=coordinated_change_id
            )
        )

        revision_no = (
            coordinated_change.current_revision
        )

        previous_concurrency_version = (
            coordinated_change.concurrency_version
        )

        # ---------------------------------------------------------
        # Load the existing draft children.
        #
        # They were already locked by the staleness inspection; the
        # explicit select_for_update keeps the mutation boundary clear.
        # ---------------------------------------------------------

        change_requests = tuple(
            ChangeRequest.objects
            .select_for_update()
            .filter(
                coordinated_change=(
                    coordinated_change
                )
            )
            .order_by(
                "grant_id",
                "id",
            )
        )

        grant_ids = tuple(
            change_request.grant_id
            for change_request
            in change_requests
        )

        # ---------------------------------------------------------
        # Normalize the authoritative review snapshot.
        #
        # The future view will obtain this mapping from a signed token.
        # The workflow service still validates its shape independently.
        # ---------------------------------------------------------

        try:
            reviewed_grant_items = tuple(
                reviewed_authoritative_values_by_grant.items()
            )
        except AttributeError as exc:
            raise CoordinatedChangeValidationError(
                "Reviewed authoritative values must be provided by "
                "grant ID."
            ) from exc

        normalized_reviewed_values = {}

        for raw_grant_id, raw_reviewed_values in (
            reviewed_grant_items
        ):
            grant_id = str(
                raw_grant_id
            ).strip()

            if not grant_id:
                raise CoordinatedChangeValidationError(
                    "Every reviewed authoritative value set must "
                    "identify a grant."
                )

            if grant_id in normalized_reviewed_values:
                raise CoordinatedChangeValidationError(
                    "The reviewed authoritative snapshot contains "
                    f"grant {grant_id} more than once."
                )

            try:
                reviewed_field_items = tuple(
                    raw_reviewed_values.items()
                )
            except AttributeError as exc:
                raise CoordinatedChangeValidationError(
                    f"Reviewed authoritative values for grant "
                    f"{grant_id} must be provided by field name."
                ) from exc

            reviewed_field_names = {
                str(field_name)
                for field_name, _
                in reviewed_field_items
            }

            required_field_names = set(
                GRANT_BASIC_INFORMATION_CHANGE_FIELDS
            )

            missing_field_names = sorted(
                required_field_names
                - reviewed_field_names
            )

            unexpected_field_names = sorted(
                reviewed_field_names
                - required_field_names
            )

            if missing_field_names:
                raise CoordinatedChangeValidationError(
                    f"Reviewed authoritative values for grant "
                    f"{grant_id} are incomplete. Missing fields: "
                    + ", ".join(
                        missing_field_names
                    )
                    + "."
                )

            if unexpected_field_names:
                raise CoordinatedChangeValidationError(
                    f"Reviewed authoritative values for grant "
                    f"{grant_id} contain unsupported fields: "
                    + ", ".join(
                        unexpected_field_names
                    )
                    + "."
                )

            normalized_reviewed_values[
                grant_id
            ] = {
                str(field_name): (
                    serialize_change_request_value(
                        value
                    )
                )
                for field_name, value
                in reviewed_field_items
            }

        reviewed_grant_ids = set(
            normalized_reviewed_values
        )

        expected_grant_ids = set(
            grant_ids
        )

        missing_reviewed_grant_ids = sorted(
            expected_grant_ids
            - reviewed_grant_ids
        )

        unexpected_reviewed_grant_ids = sorted(
            reviewed_grant_ids
            - expected_grant_ids
        )

        if missing_reviewed_grant_ids:
            raise CoordinatedChangeValidationError(
                "The reviewed authoritative snapshot does not include "
                "every grant currently in the coordinated draft. "
                "Missing grant IDs: "
                + ", ".join(
                    missing_reviewed_grant_ids
                )
                + "."
            )

        if unexpected_reviewed_grant_ids:
            raise CoordinatedChangeValidationError(
                "The reviewed authoritative snapshot contains grants "
                "that are not currently in the coordinated draft. "
                "Unexpected grant IDs: "
                + ", ".join(
                    unexpected_reviewed_grant_ids
                )
                + "."
            )

        # ---------------------------------------------------------
        # Confirm that authoritative Form1 still exactly matches the
        # state that the editor reviewed.
        #
        # We compare all 14 fields for every current child, not merely
        # the fields that were stale during the original review. That
        # prevents an unrelated authoritative change from slipping
        # through between review and confirmation.
        # ---------------------------------------------------------

        authoritative_grants = tuple(
            Form1.objects
            .select_for_update()
            .filter(
                grant_id__in=grant_ids
            )
            .order_by("grant_id")
        )

        authoritative_grants_by_id = {
            grant.grant_id: grant
            for grant in authoritative_grants
        }

        missing_authoritative_grant_ids = sorted(
            expected_grant_ids
            - set(
                authoritative_grants_by_id
            )
        )

        if missing_authoritative_grant_ids:
            raise CoordinatedChangeValidationError(
                "Every coordinated draft child must reference an "
                "existing authoritative grant. Missing grant IDs: "
                + ", ".join(
                    missing_authoritative_grant_ids
                )
                + "."
            )

        review_mismatches = []

        for grant_id in grant_ids:

            grant = (
                authoritative_grants_by_id[
                    grant_id
                ]
            )

            reviewed_values = (
                normalized_reviewed_values[
                    grant_id
                ]
            )

            for field_name in (
                GRANT_BASIC_INFORMATION_CHANGE_FIELDS
            ):
                authoritative_value = (
                    serialize_change_request_value(
                        getattr(
                            grant,
                            field_name,
                        )
                    )
                )

                reviewed_value = (
                    reviewed_values[
                        field_name
                    ]
                )

                if (
                    authoritative_value
                    != reviewed_value
                ):
                    review_mismatches.append(
                        CoordinatedDraftReviewedAuthoritativeMismatch(
                            grant_id=grant_id,
                            field_name=field_name,
                            reviewed_value=(
                                reviewed_value
                            ),
                            authoritative_value=(
                                authoritative_value
                            ),
                        )
                    )

        if review_mismatches:
            raise CoordinatedDraftReviewMismatchError(
                mismatches=review_mismatches
            )

        # ---------------------------------------------------------
        # Prepare only the stale baseline mutations.
        #
        # IMPORTANT:
        #
        # proposed_value=None remains None.
        #
        # An old stale baseline is NEVER promoted into proposed_value.
        #
        # An explicit proposal is preserved unless the new
        # authoritative value has caught up to that proposal.
        # ---------------------------------------------------------

        change_requests_by_id = {
            change_request.id: (
                change_request
            )
            for change_request
            in change_requests
        }

        snapshots_by_request_and_field = {}

        for change_request in change_requests:

            snapshots_by_field = (
                get_basic_information_revision_snapshots(
                    change_request,
                    revision_no=revision_no,
                )
            )

            for field_name, snapshot in (
                snapshots_by_field.items()
            ):
                snapshots_by_request_and_field[
                    (
                        change_request.id,
                        field_name,
                    )
                ] = snapshot

        snapshot_updates = []

        for difference in (
            staleness_result.baseline_differences
        ):
            change_request = (
                change_requests_by_id.get(
                    difference.change_request_id
                )
            )

            if change_request is None:
                raise CoordinatedChangeValidationError(
                    "The coordinated draft changed while its "
                    "authoritative review was being processed."
                )

            snapshot_key = (
                change_request.id,
                difference.field_name,
            )

            snapshot = (
                snapshots_by_request_and_field.get(
                    snapshot_key
                )
            )

            if snapshot is None:
                raise CoordinatedChangeValidationError(
                    f"Grant {difference.grant_id} does not contain "
                    f"the expected {difference.field_name} draft "
                    "snapshot."
                )

            new_current_value = (
                difference.authoritative_value
            )

            snapshot.current_value = (
                new_current_value
            )

            if (
                snapshot.proposed_value
                is not None
                and snapshot.proposed_value
                == new_current_value
            ):
                snapshot.proposed_value = None

            snapshot_updates.append(
                snapshot
            )

        if snapshot_updates:
            ChangeRequestField.objects.bulk_update(
                snapshot_updates,
                [
                    "current_value",
                    "proposed_value",
                ],
            )

        # ---------------------------------------------------------
        # Rebase is mutable draft work, not a new formal revision.
        # Advance only the package concurrency version.
        # ---------------------------------------------------------

        coordinated_change.concurrency_version = (
            previous_concurrency_version + 1
        )

        coordinated_change.save(
            update_fields=[
                "concurrency_version",
            ]
        )

        return CoordinatedDraftRebaseResult(
            coordinated_change_id=(
                coordinated_change.id
            ),
            status=(
                coordinated_change.status
            ),
            revision_no=revision_no,
            previous_concurrency_version=(
                previous_concurrency_version
            ),
            concurrency_version=(
                coordinated_change.concurrency_version
            ),
            change_request_ids=tuple(
                change_request.id
                for change_request
                in change_requests
            ),
            grant_ids=grant_ids,
            rebased_differences=(
                staleness_result.baseline_differences
            ),
        )


def submit_coordinated_basic_information_draft(
        *,
        coordinated_change_id,
        expected_concurrency_version,
        submitted_by,
):
    """
    Formally submit an existing coordinated Basic Information draft.

    Submission is an atomic transition from DRAFT to PENDING.

    Before any formal submission state is written:

      1. draft concurrency is checked;
      2. saved draft baselines are compared with authoritative Form1;
      3. the complete coordinated package passes submit-time structural,
         business, coordinated-relationship, and final GL-state
         validation.

    A successful initial submission:

      - does not create a new revision;
      - does not rewrite ChangeRequestField snapshots;
      - does not modify authoritative Form1;
      - records one shared submission timestamp;
      - records the actual submitter on the coordinated package and
        every child Change Request;
      - moves the coordinated package and every child to PENDING; and
      - increments the package concurrency version exactly once.
    """
    if (
        submitted_by is None
        or getattr(submitted_by, "pk", None) is None
    ):
        raise CoordinatedChangeValidationError(
            "A saved user is required to submit a coordinated draft."
        )

    with transaction.atomic():

        # ---------------------------------------------------------
        # Concurrency and authoritative-baseline checks come first.
        #
        # This locks the coordinated package, its current children,
        # and the authoritative Form1 rows represented by the draft.
        # ---------------------------------------------------------

        staleness_result = (
            inspect_coordinated_draft_staleness(
                coordinated_change_id=(
                    coordinated_change_id
                ),
                expected_concurrency_version=(
                    expected_concurrency_version
                ),
                lock_records=True,
            )
        )

        if staleness_result.has_authoritative_changes:
            raise CoordinatedDraftAuthoritativeChangeError(
                staleness_result=staleness_result
            )

        coordinated_change = (
            CoordinatedChange.objects
            .select_for_update()
            .get(
                pk=coordinated_change_id
            )
        )

        previous_concurrency_version = (
            coordinated_change.concurrency_version
        )

        revision_no = (
            coordinated_change.current_revision
        )

        # ---------------------------------------------------------
        # An initial DRAFT must not already contain formal submission
        # history.
        # ---------------------------------------------------------

        if (
            coordinated_change.submitted_by_id is not None
            or coordinated_change.submitted_at is not None
        ):
            raise CoordinatedChangeValidationError(
                "This coordinated draft already contains submission "
                "history and cannot be submitted as an initial draft."
            )

        # ---------------------------------------------------------
        # Full submit-time validation.
        #
        # This enforces package structure, complete child proposal
        # validity, coordinated GL relationship, and the hypothetical
        # final GL state while all relevant records remain locked.
        # ---------------------------------------------------------

        validation_result = (
            validate_coordinated_basic_information_change(
                coordinated_change,
                lock_records=True,
            )
        )

        change_requests = tuple(
            ChangeRequest.objects
            .select_for_update()
            .filter(
                coordinated_change=(
                    coordinated_change
                )
            )
            .order_by(
                "grant_id",
                "id",
            )
        )

        validation_change_request_ids = {
            child.change_request_id
            for child in validation_result.children
        }

        actual_change_request_ids = {
            change_request.id
            for change_request in change_requests
        }

        if (
            actual_change_request_ids
            != validation_change_request_ids
        ):
            raise CoordinatedChangeValidationError(
                "The coordinated draft membership changed while "
                "submission was being validated."
            )

        for change_request in change_requests:
            if (
                change_request.submitted_by_id is not None
                or change_request.submitted_at is not None
            ):
                raise CoordinatedChangeValidationError(
                    f"Child Change Request #{change_request.id} "
                    "already contains submission history."
                )

        # ---------------------------------------------------------
        # One formal submission event uses one timestamp everywhere.
        # ---------------------------------------------------------

        submitted_at = timezone.now()

        for change_request in change_requests:
            change_request.status = (
                ChangeRequest.Status.PENDING
            )
            change_request.submitted_by_id = (
                submitted_by.pk
            )
            change_request.submitted_at = (
                submitted_at
            )

        ChangeRequest.objects.bulk_update(
            change_requests,
            [
                "status",
                "submitted_by",
                "submitted_at",
            ],
        )

        coordinated_change.status = (
            CoordinatedChange.Status.PENDING
        )
        coordinated_change.submitted_by_id = (
            submitted_by.pk
        )
        coordinated_change.submitted_at = (
            submitted_at
        )
        coordinated_change.concurrency_version = (
            previous_concurrency_version + 1
        )

        coordinated_change.save(
            update_fields=[
                "status",
                "submitted_by",
                "submitted_at",
                "concurrency_version",
            ]
        )

        return CoordinatedDraftSubmissionResult(
            coordinated_change_id=(
                coordinated_change.id
            ),
            status=(
                coordinated_change.status
            ),
            revision_no=revision_no,
            previous_concurrency_version=(
                previous_concurrency_version
            ),
            concurrency_version=(
                coordinated_change.concurrency_version
            ),
            change_request_ids=tuple(
                change_request.id
                for change_request
                in change_requests
            ),
            grant_ids=tuple(
                change_request.grant_id
                for change_request
                in change_requests
            ),
            submitted_by_id=(
                submitted_by.pk
            ),
            submitted_at=(
                submitted_at
            ),
        )


def approve_coordinated_basic_information_change(
        *,
        coordinated_change_id,
        approver,
):
    """
    Record one approval for a submitted coordinated Basic Information
    package.

    Approval #1:
      - fully revalidates the coordinated package;
      - records the same approver on every child revision;
      - leaves the parent and all children PENDING.

    Approval #2:
      - fully revalidates the entire package again;
      - records the second approver on every child revision;
      - applies every validated Form1 change inside one transaction;
      - rematches affected GL Award Codes only after every Form1 grant
        has reached its final coordinated state;
      - marks every child APPROVED and the parent APPLIED.

    Child approval histories must remain synchronized. A package with
    mixed child approval histories is rejected rather than repaired
    silently.

    If an authoritative child baseline mismatch is detected, the
    existing Change Request integrity workflow is used for that child
    and the coordinated approval is blocked.
    """
    if (
        approver is None
        or getattr(approver, "pk", None) is None
    ):
        raise CoordinatedChangeApprovalError(
            "A saved user is required to approve a coordinated package."
        )

    blocked_integrity_issue_id = None
    blocked_integrity_issue_created = False
    approval_result = None

    with transaction.atomic():

        try:
            coordinated_change = (
                CoordinatedChange.objects
                .select_for_update()
                .get(pk=coordinated_change_id)
            )

        except CoordinatedChange.DoesNotExist as exc:
            raise CoordinatedChangeApprovalError(
                "The coordinated package does not exist."
            ) from exc

        if (
            coordinated_change.status
            != CoordinatedChange.Status.PENDING
        ):
            raise CoordinatedChangeApprovalError(
                "This coordinated package is no longer pending approval."
            )

        if coordinated_change.applied_at is not None:
            raise CoordinatedChangeApprovalError(
                "This pending coordinated package already contains "
                "application history."
            )

        # -----------------------------------------------------
        # Lock and structurally validate the package children.
        # -----------------------------------------------------

        structure_result = (
            validate_coordinated_change_structure(
                coordinated_change,
                lock_children=True,
            )
        )

        change_requests = (
            structure_result.change_requests
        )

        revision_no = (
            structure_result.revision_no
        )

        # -----------------------------------------------------
        # Revision 1 submission metadata must agree across the
        # parent and all children.
        # -----------------------------------------------------

        if revision_no == 1:

            if (
                coordinated_change.submitted_by_id is None
                or coordinated_change.submitted_at is None
            ):
                raise CoordinatedChangeApprovalError(
                    "This coordinated package has incomplete "
                    "submission history."
                )

            submission_metadata_matches = all(
                (
                    change_request.submitted_by_id
                    == coordinated_change.submitted_by_id
                    and change_request.submitted_at
                    == coordinated_change.submitted_at
                )
                for change_request
                in change_requests
            )

            if not submission_metadata_matches:
                raise CoordinatedChangeApprovalError(
                    "The coordinated package and its child Change "
                    "Requests do not have synchronized submission "
                    "history."
                )

        # -----------------------------------------------------
        # Existing integrity incidents block the package.
        # -----------------------------------------------------

        for change_request in change_requests:

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
                break

        if blocked_integrity_issue_id is None:

            # -------------------------------------------------
            # Every child must identify the same submitter for
            # the current formal revision.
            # -------------------------------------------------

            revision_submitter_ids = set()

            for change_request in change_requests:

                try:
                    revision_submitter_id = (
                        get_revision_submitter_id(
                            change_request
                        )
                    )

                except ChangeRequestApprovalError as exc:
                    raise CoordinatedChangeApprovalError(
                        f"Child Change Request "
                        f"#{change_request.id} has invalid "
                        "submission history: "
                        + str(exc)
                    ) from exc

                revision_submitter_ids.add(
                    revision_submitter_id
                )

            if len(revision_submitter_ids) != 1:
                raise CoordinatedChangeApprovalError(
                    "The coordinated package children do not identify "
                    "the same submitter for the current revision."
                )

            revision_submitter_id = next(
                iter(revision_submitter_ids)
            )

            if revision_submitter_id == approver.pk:
                raise CoordinatedChangeApprovalError(
                    "You cannot approve a coordinated package revision "
                    "that you submitted."
                )

            # -------------------------------------------------
            # Approval history is a package-level invariant.
            #
            # Every child must contain exactly the same ordered
            # list of approver user IDs for this revision.
            # -------------------------------------------------

            approval_histories = []

            for change_request in change_requests:

                approval_user_ids = tuple(
                    ChangeAction.objects
                    .filter(
                        change_request=change_request,
                        revision_no=revision_no,
                        action=(
                            ChangeAction.Action.APPROVE
                        ),
                    )
                    .order_by(
                        "acted_at",
                        "id",
                    )
                    .values_list(
                        "acted_by_id",
                        flat=True,
                    )
                )

                approval_histories.append(
                    (
                        change_request.id,
                        approval_user_ids,
                    )
                )

            prior_approval_user_ids = (
                approval_histories[0][1]
            )

            histories_are_synchronized = all(
                approval_user_ids
                == prior_approval_user_ids
                for (
                    _change_request_id,
                    approval_user_ids,
                )
                in approval_histories
            )

            if not histories_are_synchronized:
                raise CoordinatedChangeApprovalError(
                    "The coordinated package child approval histories "
                    "are not synchronized."
                )

            if len(prior_approval_user_ids) > 1:
                raise CoordinatedChangeApprovalError(
                    "This coordinated package already contains enough "
                    "approvals and cannot receive another approval "
                    "while still Pending."
                )

            if (
                prior_approval_user_ids
                and revision_submitter_id
                in prior_approval_user_ids
            ):
                raise CoordinatedChangeApprovalError(
                    "The coordinated package contains an approval "
                    "recorded by the revision submitter."
                )

            if approver.pk in prior_approval_user_ids:
                raise CoordinatedChangeApprovalError(
                    "You have already approved this coordinated "
                    "package revision."
                )

            # -------------------------------------------------
            # Fully revalidate the package before every approval.
            #
            # This validates all children together, including the
            # coordinated relationship and hypothetical final GL
            # assignment state.
            # -------------------------------------------------

            try:
                validation_result = (
                    validate_coordinated_basic_information_change(
                        coordinated_change,
                        lock_records=True,
                    )
                )

            except CoordinatedChangeChildBaselineMismatchError as exc:

                detection_result = (
                    detect_or_get_change_request_integrity_issue(
                        change_request_id=(
                            exc.change_request_id
                        ),
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

            if blocked_integrity_issue_id is None:

                actual_change_request_ids = {
                    change_request.id
                    for change_request
                    in change_requests
                }

                validation_change_request_ids = {
                    child_result.change_request_id
                    for child_result
                    in validation_result.children
                }

                if (
                    actual_change_request_ids
                    != validation_change_request_ids
                ):
                    raise CoordinatedChangeApprovalError(
                        "The coordinated package membership changed "
                        "while approval was being validated."
                    )

                # ---------------------------------------------
                # Record this package approval on every child.
                #
                # These writes are inside the same transaction.
                # A later failure will roll all of them back.
                # ---------------------------------------------

                for change_request in change_requests:

                    ChangeAction.objects.create(
                        change_request=change_request,
                        revision_no=revision_no,
                        acted_by=approver,
                        action=(
                            ChangeAction.Action.APPROVE
                        ),
                        comment="",
                    )

                approval_count = (
                    len(prior_approval_user_ids)
                    + 1
                )

                # ---------------------------------------------
                # Approval #1: audit only.
                # ---------------------------------------------

                if approval_count == 1:

                    approval_result = (
                        CoordinatedApprovalResult(
                            coordinated_change_id=(
                                coordinated_change.id
                            ),
                            status=(
                                coordinated_change.status
                            ),
                            revision_no=revision_no,
                            approval_count=1,
                            change_request_ids=tuple(
                                change_request.id
                                for change_request
                                in change_requests
                            ),
                            grant_ids=tuple(
                                change_request.grant_id
                                for change_request
                                in change_requests
                            ),
                            approver_id=approver.pk,
                            applied_at=None,
                            gl_rematch_result=None,
                        )
                    )

                # ---------------------------------------------
                # Approval #2: apply the entire package.
                # ---------------------------------------------

                elif approval_count == 2:

                    grant_ids = tuple(
                        change_request.grant_id
                        for change_request
                        in change_requests
                    )

                    grants_by_id = {
                        grant.grant_id: grant
                        for grant in (
                            Form1.objects
                            .select_for_update()
                            .filter(
                                grant_id__in=grant_ids
                            )
                            .order_by("grant_id")
                        )
                    }

                    missing_grant_ids = sorted(
                        set(grant_ids)
                        - set(grants_by_id)
                    )

                    if missing_grant_ids:
                        raise CoordinatedChangeApprovalError(
                            "One or more coordinated grants disappeared "
                            "while final approval was being applied. "
                            "Missing grant IDs: "
                            + ", ".join(
                                missing_grant_ids
                            )
                            + "."
                        )

                    affected_award_codes = set()

                    # -----------------------------------------
                    # Apply ALL Form1 changes first.
                    #
                    # Do not call
                    # _apply_validated_basic_information_values()
                    # here because that standalone helper rematches
                    # GL immediately after each individual grant.
                    # -----------------------------------------

                    for child_result in (
                        validation_result.children
                    ):

                        grant = grants_by_id[
                            child_result.grant_id
                        ]

                        old_award_code = str(
                            child_result.current_award_code
                            or ""
                        ).strip()

                        if old_award_code:
                            affected_award_codes.add(
                                old_award_code
                            )

                        child_validation = (
                            child_result.validation_result
                        )

                        if not child_validation.changed_fields:
                            raise CoordinatedChangeApprovalError(
                                f"Child Change Request "
                                f"#{child_result.change_request_id} "
                                "contains no Basic Information changes."
                            )

                        for field_name in (
                            child_validation.changed_fields
                        ):
                            setattr(
                                grant,
                                field_name,
                                child_validation
                                .proposed_values[
                                    field_name
                                ],
                            )

                        grant.save(
                            update_fields=list(
                                child_validation
                                .changed_fields
                            )
                        )

                        new_award_code = str(
                            grant.internal_award_code
                            or ""
                        ).strip()

                        if new_award_code:
                            affected_award_codes.add(
                                new_award_code
                            )

                    # -----------------------------------------
                    # Every Form1 row is now in the validated
                    # final coordinated state.
                    #
                    # Perform ONE GL rematch using the union of
                    # all old and new Award Codes.
                    # -----------------------------------------

                    if affected_award_codes:
                        gl_rematch_result = (
                            rematch_gl_expenditures(
                                award_codes=(
                                    affected_award_codes
                                )
                            )
                        )
                    else:
                        gl_rematch_result = None

                    # -----------------------------------------
                    # Finalize child and parent statuses only
                    # after all Form1 and GL work succeeds.
                    # -----------------------------------------

                    for change_request in change_requests:
                        change_request.status = (
                            ChangeRequest.Status.APPROVED
                        )

                    ChangeRequest.objects.bulk_update(
                        change_requests,
                        ["status"],
                    )

                    applied_at = timezone.now()

                    coordinated_change.status = (
                        CoordinatedChange.Status.APPLIED
                    )

                    coordinated_change.applied_at = (
                        applied_at
                    )

                    coordinated_change.save(
                        update_fields=[
                            "status",
                            "applied_at",
                        ]
                    )

                    approval_result = (
                        CoordinatedApprovalResult(
                            coordinated_change_id=(
                                coordinated_change.id
                            ),
                            status=(
                                coordinated_change.status
                            ),
                            revision_no=revision_no,
                            approval_count=2,
                            change_request_ids=tuple(
                                change_request.id
                                for change_request
                                in change_requests
                            ),
                            grant_ids=tuple(
                                change_request.grant_id
                                for change_request
                                in change_requests
                            ),
                            approver_id=approver.pk,
                            applied_at=applied_at,
                            gl_rematch_result=(
                                gl_rematch_result
                            ),
                        )
                    )

                else:
                    raise CoordinatedChangeApprovalError(
                        "The coordinated approval count is invalid."
                    )

    # ---------------------------------------------------------
    # Match the standalone integrity workflow:
    #
    # commit the integrity incident first, then block approval
    # outside the transaction that created/fetched the incident.
    # ---------------------------------------------------------

    if blocked_integrity_issue_id is not None:
        raise ChangeRequestIntegrityBlockedError(
            blocked_integrity_issue_id,
            newly_detected=(
                blocked_integrity_issue_created
            ),
        )

    return approval_result


def return_coordinated_basic_information_change(
        *,
        coordinated_change_id,
        approver,
        comment,
):
    """
    Return a submitted coordinated Basic Information package for revision.

    The package is one workflow unit:

      - the parent and every child must currently be PENDING;
      - every child must be on the package's current revision;
      - child submission and approval histories must remain synchronized;
      - the package revision submitter cannot return their own revision;
      - an approver who already approved the current revision cannot return it;
      - existing approvals remain immutable history;
      - one RETURN action is recorded on every child;
      - the parent and every child move to RETURNED together;
      - no authoritative Form1 values are changed.

    If an authoritative child baseline mismatch is detected, the existing
    Change Request integrity workflow is used for that child and the
    coordinated Return for Revision is blocked.
    """
    feedback = (comment or "").strip()

    if (
        approver is None
        or getattr(approver, "pk", None) is None
    ):
        raise CoordinatedChangeReturnError(
            "A saved user is required to return a coordinated package."
        )

    if not feedback:
        raise CoordinatedChangeReturnError(
            "Return for Revision requires feedback."
        )

    if len(feedback) > 500:
        raise CoordinatedChangeReturnError(
            "Return for Revision feedback cannot exceed 500 characters."
        )

    blocked_integrity_issue_id = None
    blocked_integrity_issue_created = False
    return_result = None

    with transaction.atomic():

        try:
            coordinated_change = (
                CoordinatedChange.objects
                .select_for_update()
                .get(
                    pk=coordinated_change_id
                )
            )

        except CoordinatedChange.DoesNotExist as exc:
            raise CoordinatedChangeReturnError(
                "The coordinated package does not exist."
            ) from exc

        if (
            coordinated_change.status
            != CoordinatedChange.Status.PENDING
        ):
            raise CoordinatedChangeReturnError(
                "This coordinated package is no longer pending review."
            )

        revision_no = (
            coordinated_change.current_revision
        )

        #
        # Structural validation confirms:
        #   - at least two children,
        #   - parent/child revision synchronization,
        #   - parent/child status synchronization,
        #   - supported child structure,
        #   - complete formal snapshots.
        #
        validate_coordinated_change_structure(
            coordinated_change,
            lock_children=True,
        )

        change_requests = tuple(
            ChangeRequest.objects
            .select_for_update()
            .filter(
                coordinated_change=(
                    coordinated_change
                )
            )
            .order_by("id")
        )

        if not change_requests:
            raise CoordinatedChangeReturnError(
                "The coordinated package does not contain "
                "any child Change Requests."
            )

        for change_request in change_requests:
            if (
                change_request.request_type
                != ChangeRequest.RequestType.EDIT_GRANT
            ):
                raise CoordinatedChangeReturnError(
                    "Coordinated Return for Revision currently "
                    "supports only existing-grant Basic Information "
                    "Change Requests."
                )

        # -------------------------------------------------
        # An existing open integrity incident on any child
        # blocks the whole package.
        # -------------------------------------------------

        for change_request in change_requests:

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

                break

        if blocked_integrity_issue_id is None:

            # -------------------------------------------------
            # Every child must identify the same submitter for
            # the current formal package revision.
            # -------------------------------------------------

            revision_submitter_ids = set()

            for change_request in change_requests:

                try:
                    revision_submitter_id = (
                        get_revision_submitter_id(
                            change_request
                        )
                    )

                except ChangeRequestApprovalError as exc:
                    raise CoordinatedChangeReturnError(
                        f"Child Change Request "
                        f"#{change_request.id} has invalid "
                        "submission history: "
                        + str(exc)
                    ) from exc

                revision_submitter_ids.add(
                    revision_submitter_id
                )

            if len(revision_submitter_ids) != 1:
                raise CoordinatedChangeReturnError(
                    "The coordinated package children do not identify "
                    "the same submitter for the current revision."
                )

            revision_submitter_id = next(
                iter(revision_submitter_ids)
            )

            if revision_submitter_id == approver.pk:
                raise CoordinatedChangeReturnError(
                    "You cannot return a coordinated package revision "
                    "that you submitted."
                )

            # -------------------------------------------------
            # Approval history is a package-level invariant.
            #
            # Every child must contain the same ordered approver
            # sequence for this revision.
            # -------------------------------------------------

            approval_histories = []

            for change_request in change_requests:

                approval_user_ids = tuple(
                    ChangeAction.objects
                    .filter(
                        change_request=(
                            change_request
                        ),
                        revision_no=revision_no,
                        action=(
                            ChangeAction.Action.APPROVE
                        ),
                    )
                    .order_by(
                        "acted_at",
                        "id",
                    )
                    .values_list(
                        "acted_by_id",
                        flat=True,
                    )
                )

                approval_histories.append(
                    (
                        change_request.id,
                        approval_user_ids,
                    )
                )

            prior_approval_user_ids = (
                approval_histories[0][1]
            )

            histories_are_synchronized = all(
                approval_user_ids
                == prior_approval_user_ids
                for (
                    _change_request_id,
                    approval_user_ids,
                )
                in approval_histories
            )

            if not histories_are_synchronized:
                raise CoordinatedChangeReturnError(
                    "The coordinated package child approval histories "
                    "are not synchronized."
                )

            approval_count = len(
                prior_approval_user_ids
            )

            if approval_count >= 2:
                raise CoordinatedChangeReturnError(
                    "A fully approved coordinated package revision "
                    "cannot be returned for changes."
                )

            if (
                revision_submitter_id
                in prior_approval_user_ids
            ):
                raise CoordinatedChangeReturnError(
                    "The coordinated package contains an approval "
                    "recorded by the revision submitter."
                )

            if approver.pk in prior_approval_user_ids:
                raise CoordinatedChangeReturnError(
                    "You have already approved this coordinated "
                    "package revision and cannot return the same "
                    "revision for changes."
                )

            # -------------------------------------------------
            # A PENDING current revision must not already contain
            # a RETURN action.
            # -------------------------------------------------

            for change_request in change_requests:

                existing_return_count = (
                    ChangeAction.objects
                    .filter(
                        change_request=(
                            change_request
                        ),
                        revision_no=revision_no,
                        action=(
                            ChangeAction.Action.RETURN
                        ),
                    )
                    .count()
                )

                if existing_return_count:
                    raise CoordinatedChangeReturnError(
                        f"Child Change Request "
                        f"#{change_request.id} already contains "
                        "a Return for Revision action for the "
                        "current revision."
                    )

            # -------------------------------------------------
            # Lock every authoritative grant before checking
            # formal revision baselines.
            #
            # Ordinary Return validates the immutable formal
            # revision baseline, just like standalone Return.
            # It does NOT rewrite or rebase snapshots.
            # -------------------------------------------------

            grant_ids = tuple(
                change_request.grant_id
                for change_request
                in change_requests
            )

            grants = tuple(
                Form1.objects
                .select_for_update()
                .filter(
                    grant_id__in=grant_ids
                )
            )

            grants_by_id = {
                grant.grant_id: grant
                for grant in grants
            }

            if len(grants_by_id) != len(
                set(grant_ids)
            ):
                raise CoordinatedChangeReturnError(
                    "One or more authoritative grants belonging "
                    "to the coordinated package could not be found."
                )

            # -------------------------------------------------
            # Validate every child before creating any RETURN
            # action or changing any package status.
            # -------------------------------------------------

            for change_request in change_requests:

                grant = grants_by_id[
                    change_request.grant_id
                ]

                try:
                    validate_basic_information_revision_baseline(
                        change_request,
                        grant=grant,
                    )

                except ChangeRequestBaselineMismatchError:

                    detection_result = (
                        detect_or_get_change_request_integrity_issue(
                            change_request_id=(
                                change_request.id
                            ),
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

                    break

            # -------------------------------------------------
            # Only after every child baseline has passed do we
            # write package-level RETURN history and statuses.
            # -------------------------------------------------

            if blocked_integrity_issue_id is None:

                for change_request in change_requests:

                    ChangeAction.objects.create(
                        change_request=(
                            change_request
                        ),
                        revision_no=revision_no,
                        acted_by=approver,
                        action=(
                            ChangeAction.Action.RETURN
                        ),
                        comment=feedback,
                    )

                for change_request in change_requests:
                    change_request.status = (
                        ChangeRequest.Status.RETURNED
                    )

                ChangeRequest.objects.bulk_update(
                    change_requests,
                    ["status"],
                )

                coordinated_change.status = (
                    CoordinatedChange.Status.RETURNED
                )

                coordinated_change.save(
                    update_fields=[
                        "status",
                    ]
                )

                return_result = (
                    CoordinatedReturnResult(
                        coordinated_change_id=(
                            coordinated_change.id
                        ),
                        status=(
                            coordinated_change.status
                        ),
                        revision_no=revision_no,
                        approval_count=(
                            approval_count
                        ),
                        change_request_ids=tuple(
                            change_request.id
                            for change_request
                            in change_requests
                        ),
                        grant_ids=tuple(
                            change_request.grant_id
                            for change_request
                            in change_requests
                        ),
                        returned_by_id=(
                            approver.pk
                        ),
                    )
                )

    #
    # Do not raise this inside the transaction. The integrity
    # incident must commit first, exactly like the standalone
    # workflow.
    #
    if blocked_integrity_issue_id is not None:
        raise ChangeRequestIntegrityBlockedError(
            blocked_integrity_issue_id,
            newly_detected=(
                blocked_integrity_issue_created
            ),
        )

    return return_result


def deny_coordinated_basic_information_change(
        *,
        coordinated_change_id,
        approver,
        comment,
):
    """
    Deny a submitted coordinated Basic Information package.

    Denial is terminal for the whole package:

      - the parent and every child must currently be PENDING;
      - every child must be on the package's current revision;
      - child submission and approval histories must be synchronized;
      - the package revision submitter cannot deny their own revision;
      - an approver who already approved the current revision cannot deny it;
      - existing approvals remain immutable history;
      - a denial reason is required;
      - one DENY action is recorded on every child;
      - the parent and every child move to DENIED together;
      - authoritative Form1 values are not changed.
    """
    feedback = (comment or "").strip()

    if (
        approver is None
        or getattr(approver, "pk", None) is None
    ):
        raise CoordinatedChangeDenyError(
            "A saved user is required to deny a coordinated package."
        )

    if not feedback:
        raise CoordinatedChangeDenyError(
            "Denial requires a reason."
        )

    if len(feedback) > 500:
        raise CoordinatedChangeDenyError(
            "Denial reason cannot exceed 500 characters."
        )

    with transaction.atomic():

        try:
            coordinated_change = (
                CoordinatedChange.objects
                .select_for_update()
                .get(
                    pk=coordinated_change_id
                )
            )

        except CoordinatedChange.DoesNotExist as exc:
            raise CoordinatedChangeDenyError(
                "The coordinated package does not exist."
            ) from exc

        if (
            coordinated_change.status
            != CoordinatedChange.Status.PENDING
        ):
            raise CoordinatedChangeDenyError(
                "This coordinated package is no longer pending review."
            )

        if coordinated_change.applied_at is not None:
            raise CoordinatedChangeDenyError(
                "An applied coordinated package cannot be denied."
            )

        revision_no = (
            coordinated_change.current_revision
        )

        # -----------------------------------------------------
        # Validate and lock the complete package boundary.
        #
        # This protects:
        #   - minimum child count,
        #   - parent/child revision synchronization,
        #   - parent/child status synchronization,
        #   - supported child structure,
        #   - complete current-revision snapshots.
        # -----------------------------------------------------

        try:
            structure_result = (
                validate_coordinated_change_structure(
                    coordinated_change,
                    lock_children=True,
                )
            )

        except ChangeRequestValidationError as exc:
            raise CoordinatedChangeDenyError(
                str(exc)
            ) from exc

        change_requests = tuple(
            structure_result.change_requests
        )

        if not change_requests:
            raise CoordinatedChangeDenyError(
                "The coordinated package does not contain "
                "any child Change Requests."
            )

        for change_request in change_requests:
            if (
                change_request.request_type
                != ChangeRequest.RequestType.EDIT_GRANT
            ):
                raise CoordinatedChangeDenyError(
                    "Coordinated Deny currently supports only "
                    "existing-grant Basic Information Change Requests."
                )

        # -----------------------------------------------------
        # Any existing OPEN child integrity incident blocks
        # normal package workflow actions, including Deny.
        # -----------------------------------------------------

        for change_request in change_requests:

            existing_integrity_issue = (
                get_open_change_request_integrity_issue(
                    change_request,
                    lock=True,
                )
            )

            if existing_integrity_issue is not None:
                raise ChangeRequestIntegrityBlockedError(
                    existing_integrity_issue.id,
                    newly_detected=False,
                )

        # -----------------------------------------------------
        # Every child must identify the same submitter for
        # this formal package revision.
        # -----------------------------------------------------

        revision_submitter_ids = set()

        for change_request in change_requests:

            try:
                revision_submitter_id = (
                    get_revision_submitter_id(
                        change_request
                    )
                )

            except ChangeRequestApprovalError as exc:
                raise CoordinatedChangeDenyError(
                    f"Child Change Request "
                    f"#{change_request.id} has invalid "
                    "submission history: "
                    + str(exc)
                ) from exc

            revision_submitter_ids.add(
                revision_submitter_id
            )

        if len(revision_submitter_ids) != 1:
            raise CoordinatedChangeDenyError(
                "The coordinated package children do not identify "
                "the same submitter for the current revision."
            )

        revision_submitter_id = next(
            iter(revision_submitter_ids)
        )

        if revision_submitter_id == approver.pk:
            raise CoordinatedChangeDenyError(
                "You cannot deny a coordinated package revision "
                "that you submitted."
            )

        # -----------------------------------------------------
        # Approval history is a package-level invariant.
        #
        # Every child must contain the same ordered approver
        # sequence for the current revision.
        # -----------------------------------------------------

        approval_histories = []

        for change_request in change_requests:

            approval_user_ids = tuple(
                ChangeAction.objects
                .filter(
                    change_request=change_request,
                    revision_no=revision_no,
                    action=(
                        ChangeAction.Action.APPROVE
                    ),
                )
                .order_by(
                    "acted_at",
                    "id",
                )
                .values_list(
                    "acted_by_id",
                    flat=True,
                )
            )

            approval_histories.append(
                (
                    change_request.id,
                    approval_user_ids,
                )
            )

        prior_approval_user_ids = (
            approval_histories[0][1]
        )

        histories_are_synchronized = all(
            approval_user_ids
            == prior_approval_user_ids
            for (
                _change_request_id,
                approval_user_ids,
            )
            in approval_histories
        )

        if not histories_are_synchronized:
            raise CoordinatedChangeDenyError(
                "The coordinated package child approval histories "
                "are not synchronized."
            )

        approval_count = len(
            prior_approval_user_ids
        )

        if approval_count >= 2:
            raise CoordinatedChangeDenyError(
                "A fully approved coordinated package revision "
                "cannot be denied."
            )

        if (
            revision_submitter_id
            in prior_approval_user_ids
        ):
            raise CoordinatedChangeDenyError(
                "The coordinated package contains an approval "
                "recorded by the revision submitter."
            )

        if approver.pk in prior_approval_user_ids:
            raise CoordinatedChangeDenyError(
                "You have already approved this coordinated "
                "package revision and cannot deny the same revision."
            )

        # -----------------------------------------------------
        # A PENDING package must not already contain a DENY
        # action for the current revision.
        # -----------------------------------------------------

        for change_request in change_requests:

            existing_deny_count = (
                ChangeAction.objects
                .filter(
                    change_request=change_request,
                    revision_no=revision_no,
                    action=(
                        ChangeAction.Action.DENY
                    ),
                )
                .count()
            )

            if existing_deny_count:
                raise CoordinatedChangeDenyError(
                    f"Child Change Request "
                    f"#{change_request.id} already contains "
                    "a Deny action for the current revision."
                )

        # -----------------------------------------------------
        # Record the same package-level denial on every child.
        #
        # All writes occur inside this transaction. Any failure
        # rolls back every child action and every status change.
        # -----------------------------------------------------

        for change_request in change_requests:

            ChangeAction.objects.create(
                change_request=change_request,
                revision_no=revision_no,
                acted_by=approver,
                action=ChangeAction.Action.DENY,
                comment=feedback,
            )

        for change_request in change_requests:
            change_request.status = (
                ChangeRequest.Status.DENIED
            )

        ChangeRequest.objects.bulk_update(
            change_requests,
            ["status"],
        )

        coordinated_change.status = (
            CoordinatedChange.Status.DENIED
        )

        coordinated_change.save(
            update_fields=[
                "status",
            ]
        )

        return CoordinatedDenyResult(
            coordinated_change_id=(
                coordinated_change.id
            ),
            status=coordinated_change.status,
            revision_no=revision_no,
            approval_count=approval_count,
            change_request_ids=tuple(
                change_request.id
                for change_request
                in change_requests
            ),
            grant_ids=tuple(
                change_request.grant_id
                for change_request
                in change_requests
            ),
            denied_by_id=approver.pk,
        )


def resubmit_coordinated_basic_information_change(
        *,
        coordinated_change_id,
        resubmitter,
        proposed_form_data_by_grant,
        comment="",
):
    """
    Create the next formal revision of a RETURNED coordinated Basic
    Information package.

    Every child is validated against its own governing authoritative
    baseline:

      - ordinary Return for Revision:
            current formal revision snapshot;

      - Administrator integrity disposition:
            latest applicable DISPOSITION checkpoint.

    All child proposals are then validated together as one coordinated
    package, including coordinated GL relationship and hypothetical final
    GL assignment state.

    No next-revision snapshot or RESUBMIT action is written until all
    children and the entire package have passed validation.

    A successful resubmission:
      - preserves the previous revision unchanged;
      - creates a complete next-revision snapshot for every child;
      - records the same resubmitter on every child through one RESUBMIT
        action for the new revision;
      - advances the parent and every child to the same revision;
      - returns the parent and every child to PENDING;
      - starts the new revision with zero approvals.
    """
    if (
        resubmitter is None
        or getattr(resubmitter, "pk", None) is None
    ):
        raise CoordinatedChangeResubmitError(
            "A saved user is required to resubmit a coordinated package."
        )

    resubmission_comment = (comment or "").strip()

    if len(resubmission_comment) > 500:
        raise CoordinatedChangeResubmitError(
            "Resubmission comments cannot exceed 500 characters."
        )

    if not isinstance(
        proposed_form_data_by_grant,
        dict,
    ):
        raise CoordinatedChangeResubmitError(
            "Coordinated resubmission proposal data is invalid."
        )

    blocked_integrity_issue_id = None
    blocked_integrity_issue_created = False
    resubmit_result = None

    with transaction.atomic():

        try:
            coordinated_change = (
                CoordinatedChange.objects
                .select_for_update()
                .get(
                    pk=coordinated_change_id
                )
            )

        except CoordinatedChange.DoesNotExist as exc:
            raise CoordinatedChangeResubmitError(
                "The coordinated package does not exist."
            ) from exc

        if (
            coordinated_change.status
            != CoordinatedChange.Status.RETURNED
        ):
            raise CoordinatedChangeResubmitError(
                "Only a coordinated package that is Returned for "
                "Revision can be resubmitted."
            )

        if coordinated_change.applied_at is not None:
            raise CoordinatedChangeResubmitError(
                "An applied coordinated package cannot be resubmitted."
            )

        # -----------------------------------------------------
        # Lock and structurally validate the package.
        #
        # The existing parent/child status mapping requires a
        # RETURNED parent to contain RETURNED children.
        # -----------------------------------------------------

        try:
            structure_result = (
                validate_coordinated_change_structure(
                    coordinated_change,
                    lock_children=True,
                )
            )

        except ChangeRequestValidationError as exc:
            raise CoordinatedChangeResubmitError(
                str(exc)
            ) from exc

        change_requests = tuple(
            structure_result.change_requests
        )

        previous_revision_no = (
            structure_result.revision_no
        )

        if previous_revision_no < 1:
            raise CoordinatedChangeResubmitError(
                "The coordinated package does not have a valid "
                "formal revision."
            )

        new_revision_no = (
            previous_revision_no + 1
        )

        grant_ids = tuple(
            change_request.grant_id
            for change_request
            in change_requests
        )

        expected_grant_ids = set(
            grant_ids
        )

        proposed_grant_ids = {
            str(grant_id).strip()
            for grant_id
            in proposed_form_data_by_grant.keys()
        }

        if (
            proposed_grant_ids
            != expected_grant_ids
        ):
            missing_grant_ids = sorted(
                expected_grant_ids
                - proposed_grant_ids
            )

            unexpected_grant_ids = sorted(
                proposed_grant_ids
                - expected_grant_ids
            )

            details = []

            if missing_grant_ids:
                details.append(
                    "missing grants: "
                    + ", ".join(
                        missing_grant_ids
                    )
                )

            if unexpected_grant_ids:
                details.append(
                    "unexpected grants: "
                    + ", ".join(
                        unexpected_grant_ids
                    )
                )

            raise CoordinatedChangeResubmitError(
                "The coordinated resubmission must contain exactly "
                "the grants already belonging to the package"
                + (
                    " ("
                    + "; ".join(details)
                    + ")"
                    if details
                    else ""
                )
                + "."
            )

        # -----------------------------------------------------
        # Lock every authoritative package grant together.
        # -----------------------------------------------------

        grants_by_id = {
            grant.grant_id: grant
            for grant in (
                Form1.objects
                .select_for_update()
                .filter(
                    grant_id__in=grant_ids
                )
                .order_by(
                    "grant_id"
                )
            )
        }

        missing_authoritative_grants = sorted(
            expected_grant_ids
            - set(grants_by_id)
        )

        if missing_authoritative_grants:
            raise CoordinatedChangeResubmitError(
                "One or more authoritative coordinated grants no longer "
                "exist. Missing grant IDs: "
                + ", ".join(
                    missing_authoritative_grants
                )
                + "."
            )
        # -----------------------------------------------------
        # Validate package-level Return history.
        #
        # There are two legitimate coordinated paths:
        #
        # 1. Ordinary Approver Return:
        #       every child has exactly one synchronized RETURN
        #       action for the previous revision.
        #
        # 2. Administrator integrity disposition:
        #       the package may have no RETURN actions at all.
        #       At least one child instead has a CLOSED integrity
        #       incident for the previous revision.
        #
        # A later integrity incident can also occur after an
        # ordinary Return, so synchronized RETURN actions and
        # CLOSED integrity incidents may legitimately coexist.
        # -----------------------------------------------------

        return_histories = {}

        for change_request in change_requests:

            return_actor_ids = tuple(
                ChangeAction.objects
                .filter(
                    change_request=change_request,
                    revision_no=previous_revision_no,
                    action=ChangeAction.Action.RETURN,
                )
                .order_by(
                    "acted_at",
                    "id",
                )
                .values_list(
                    "acted_by_id",
                    flat=True,
                )
            )

            if len(return_actor_ids) > 1:
                raise CoordinatedChangeResubmitError(
                    f"Child Change Request #{change_request.id} "
                    "contains more than one Return for Revision action "
                    "for the returned revision."
                )

            return_histories[
                change_request.id
            ] = return_actor_ids

        first_return_history = (
            return_histories[
                change_requests[0].id
            ]
        )

        return_histories_are_synchronized = all(
            return_actor_ids
            == first_return_history
            for return_actor_ids
            in return_histories.values()
        )

        if not return_histories_are_synchronized:
            raise CoordinatedChangeResubmitError(
                "The coordinated package child Return for Revision "
                "histories are not synchronized."
            )

        package_return_count = len(
            first_return_history
        )

        package_has_closed_integrity_issue = (
            ChangeRequestIntegrityIssue.objects
            .filter(
                change_request__in=change_requests,
                revision_no=previous_revision_no,
                status=(
                    ChangeRequestIntegrityIssue
                    .Status
                    .CLOSED
                ),
            )
            .exists()
        )

        if (
            package_return_count == 0
            and not package_has_closed_integrity_issue
        ):
            raise CoordinatedChangeResubmitError(
                "The returned coordinated package has neither a "
                "synchronized Return for Revision history nor an "
                "Administrator integrity disposition."
            )

        # -----------------------------------------------------
        # Validate each child against its governing baseline,
        # then validate the newly proposed values.
        #
        # Nothing for the new revision is written in this loop.
        # -----------------------------------------------------

        child_validation_results = []

        for change_request in change_requests:

            if (
                change_request.request_type
                != ChangeRequest.RequestType.EDIT_GRANT
            ):
                raise CoordinatedChangeResubmitError(
                    f"Child Change Request #{change_request.id} "
                    "is not an existing-grant Basic Information request."
                )

            grant = grants_by_id[
                change_request.grant_id
            ]

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
                raise CoordinatedChangeResubmitError(
                    f"Child Change Request #{change_request.id} "
                    "has two approvals and cannot be resubmitted."
                )

            # -------------------------------------------------
            # Select and validate this child's governing
            # authoritative baseline.
            # -------------------------------------------------

            try:
                baseline_result = (
                    validate_returned_resubmission_baseline(
                        change_request,
                        grant=grant,
                        lock=True,
                    )
                )

            except ChangeRequestBaselineMismatchError:

                detection_result = (
                    detect_or_get_change_request_integrity_issue(
                        change_request_id=(
                            change_request.id
                        ),
                        detected_by=resubmitter,
                        detected_during=(
                            ChangeRequestIntegrityIssue
                            .DetectedDuring
                            .RESUBMIT
                        ),
                    )
                )

                blocked_integrity_issue_id = (
                    detection_result.integrity_issue_id
                )

                blocked_integrity_issue_created = (
                    detection_result.created
                )

                break

            except ChangeRequestIntegrityBlockedError as exc:

                blocked_integrity_issue_id = (
                    exc.integrity_issue_id
                )

                blocked_integrity_issue_created = False

                break

            except ChangeRequestValidationError as exc:
                raise CoordinatedChangeResubmitError(
                    f"Child Change Request #{change_request.id}: "
                    + str(exc)
                ) from exc

            # -------------------------------------------------
            # Confirm the child's Return / disposition history
            # agrees with the baseline source.
            # -------------------------------------------------

            if (
                    baseline_result.source
                    == RESUBMISSION_BASELINE_FORMAL_REVISION
            ):
                pass

            elif (
                baseline_result.source
                == RESUBMISSION_BASELINE_INTEGRITY_DISPOSITION
            ):
                if (
                    baseline_result.integrity_issue_id
                    is None
                ):
                    raise CoordinatedChangeResubmitError(
                        f"Child Change Request #{change_request.id} "
                        "uses an integrity disposition baseline that "
                        "does not identify its integrity incident."
                    )

            else:
                raise CoordinatedChangeResubmitError(
                    f"Child Change Request #{change_request.id} "
                    "has an unsupported authoritative baseline source."
                )

            raw_proposal = (
                proposed_form_data_by_grant.get(
                    change_request.grant_id
                )
            )

            if not isinstance(
                raw_proposal,
                dict,
            ):
                raise CoordinatedChangeResubmitError(
                    f"Grant {change_request.grant_id} has invalid "
                    "resubmission proposal data."
                )

            try:
                child_validation = (
                    _validate_basic_information_proposed_form_data(
                        change_request=change_request,
                        grant=grant,
                        proposed_form_data=raw_proposal,
                    )
                )

            except ChangeRequestValidationError as exc:
                raise CoordinatedChangeResubmitError(
                    f"Grant {change_request.grant_id}: "
                    + str(exc)
                ) from exc

            if not child_validation.changed_fields:
                raise CoordinatedChangeResubmitError(
                    f"Grant {change_request.grant_id} contains no "
                    "proposed Basic Information changes."
                )

            child_validation_results.append(
                CoordinatedChildBasicInformationValidationResult(
                    change_request_id=(
                        change_request.id
                    ),
                    grant_id=(
                        change_request.grant_id
                    ),
                    current_award_code=(
                        grant.internal_award_code
                    ),
                    current_gl_start_date=(
                        grant.internal_gl_start_date
                    ),
                    current_gl_end_date=(
                        grant.internal_gl_end_date
                    ),
                    validation_result=(
                        child_validation
                    ),
                )
            )

        # -----------------------------------------------------
        # A baseline mismatch intentionally commits its newly
        # detected integrity incident but performs no resubmit
        # writes. The exception is raised AFTER this outer
        # transaction exits normally.
        # -----------------------------------------------------

        if blocked_integrity_issue_id is None:

            if (
                len(child_validation_results)
                != len(change_requests)
            ):
                raise CoordinatedChangeResubmitError(
                    "The coordinated package was not fully validated "
                    "for resubmission."
                )

            provisional_validation_result = (
                CoordinatedBasicInformationValidationResult(
                    coordinated_change_id=(
                        coordinated_change.id
                    ),
                    revision_no=(
                        previous_revision_no
                    ),
                    children=tuple(
                        child_validation_results
                    ),
                )
            )

            # -------------------------------------------------
            # Validate all new proposals AS A PACKAGE before
            # any Revision N+1 records exist.
            # -------------------------------------------------

            try:
                validate_coordinated_gl_relationship(
                    provisional_validation_result
                )

                validate_coordinated_final_gl_state(
                    provisional_validation_result,
                    lock_outside_grants=True,
                )

            except ChangeRequestValidationError as exc:
                raise CoordinatedChangeResubmitError(
                    str(exc)
                ) from exc

            # -------------------------------------------------
            # The next revision must be completely unused on
            # every child before any writes begin.
            # -------------------------------------------------

            for change_request in change_requests:

                if (
                    ChangeRequestField.objects
                    .filter(
                        change_request=change_request,
                        revision_no=new_revision_no,
                    )
                    .exists()
                ):
                    raise CoordinatedChangeResubmitError(
                        f"Child Change Request #{change_request.id} "
                        f"already contains Revision {new_revision_no} "
                        "field snapshots."
                    )

                if (
                    ChangeAction.objects
                    .filter(
                        change_request=change_request,
                        revision_no=new_revision_no,
                    )
                    .exists()
                ):
                    raise CoordinatedChangeResubmitError(
                        f"Child Change Request #{change_request.id} "
                        f"already contains Revision {new_revision_no} "
                        "workflow actions."
                    )

            # -------------------------------------------------
            # Build ALL next-revision snapshot rows in memory.
            #
            # Current values come from authoritative Form1,
            # which has already been validated against each
            # child's governing baseline.
            # -------------------------------------------------

            all_field_snapshots = []

            validation_by_change_request_id = {
                child_result.change_request_id: (
                    child_result.validation_result
                )
                for child_result
                in child_validation_results
            }

            for change_request in change_requests:

                grant = grants_by_id[
                    change_request.grant_id
                ]

                child_validation = (
                    validation_by_change_request_id[
                        change_request.id
                    ]
                )

                field_snapshot_values = (
                    build_basic_information_field_snapshot_values(
                        grant=grant,
                        proposed_values=(
                            child_validation.proposed_values
                        ),
                    )
                )

                all_field_snapshots.extend(
                    [
                        ChangeRequestField(
                            change_request=(
                                change_request
                            ),
                            revision_no=(
                                new_revision_no
                            ),
                            field_name=(
                                snapshot["field_name"]
                            ),
                            current_value=(
                                snapshot["current_value"]
                            ),
                            proposed_value=(
                                snapshot["proposed_value"]
                            ),
                        )
                        for snapshot
                        in field_snapshot_values
                    ]
                )

            expected_snapshot_count = (
                len(change_requests)
                * len(
                    GRANT_BASIC_INFORMATION_CHANGE_FIELDS
                )
            )

            if (
                len(all_field_snapshots)
                != expected_snapshot_count
            ):
                raise CoordinatedChangeResubmitError(
                    "The coordinated next-revision snapshot is "
                    "incomplete."
                )

            # -------------------------------------------------
            # WRITE PHASE.
            #
            # Every validation above has succeeded. From here
            # onward, transaction.atomic() guarantees that a
            # later failure rolls the whole resubmission back.
            # -------------------------------------------------

            ChangeRequestField.objects.bulk_create(
                all_field_snapshots
            )

            for change_request in change_requests:

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

            ChangeRequest.objects.bulk_update(
                change_requests,
                [
                    "current_revision",
                    "status",
                ],
            )

            coordinated_change.current_revision = (
                new_revision_no
            )

            coordinated_change.status = (
                CoordinatedChange.Status.PENDING
            )

            coordinated_change.save(
                update_fields=[
                    "current_revision",
                    "status",
                ]
            )

            resubmit_result = (
                CoordinatedResubmitResult(
                    coordinated_change_id=(
                        coordinated_change.id
                    ),
                    status=(
                        coordinated_change.status
                    ),
                    previous_revision_no=(
                        previous_revision_no
                    ),
                    revision_no=(
                        new_revision_no
                    ),
                    change_request_ids=tuple(
                        change_request.id
                        for change_request
                        in change_requests
                    ),
                    grant_ids=tuple(
                        change_request.grant_id
                        for change_request
                        in change_requests
                    ),
                    resubmitter_id=(
                        resubmitter.pk
                    ),
                )
            )

    if blocked_integrity_issue_id is not None:
        raise ChangeRequestIntegrityBlockedError(
            blocked_integrity_issue_id,
            newly_detected=(
                blocked_integrity_issue_created
            ),
        )

    return resubmit_result


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

    validation_result = (
        _validate_basic_information_proposed_form_data(
            change_request=change_request,
            grant=grant,
            proposed_form_data=proposed_form_data,
        )
    )

    _validate_standalone_basic_information_overlap(
        change_request=change_request,
        proposed_values=validation_result.proposed_values,
    )

    return validation_result


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


def deny_standalone_change_request(
        *,
        change_request_id,
        approver,
        comment,
):
    """
    Deny a standalone Basic Information Change Request.

    Denial is terminal for the submitted Change Request:

      - the request must currently be PENDING;
      - the request must be standalone and EDIT_GRANT;
      - the current revision submitter cannot deny their own revision;
      - an approver who already approved the revision cannot deny it;
      - existing approval history remains immutable;
      - a denial reason is required;
      - one DENY action is recorded;
      - the Change Request moves to DENIED;
      - authoritative Form1 values are not changed.
    """
    feedback = (comment or "").strip()

    if (
        approver is None
        or getattr(approver, "pk", None) is None
    ):
        raise ChangeRequestDenyError(
            "A saved user is required to deny a Change Request."
        )

    if not feedback:
        raise ChangeRequestDenyError(
            "Denial requires a reason."
        )

    if len(feedback) > 500:
        raise ChangeRequestDenyError(
            "Denial reason cannot exceed 500 characters."
        )

    with transaction.atomic():
        try:
            change_request = (
                ChangeRequest.objects
                .select_for_update()
                .get(pk=change_request_id)
            )

        except ChangeRequest.DoesNotExist as exc:
            raise ChangeRequestDenyError(
                "The Change Request does not exist."
            ) from exc

        if change_request.coordinated_change_id is not None:
            raise ChangeRequestDenyError(
                "A coordinated Change Request cannot be denied "
                "through the standalone workflow."
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise ChangeRequestDenyError(
                "This denial service currently supports only "
                "existing-grant Basic Information Change Requests."
            )

        if change_request.status != ChangeRequest.Status.PENDING:
            raise ChangeRequestDenyError(
                "This Change Request is no longer pending approval."
            )

        existing_integrity_issue = (
            get_open_change_request_integrity_issue(
                change_request,
                lock=True,
            )
        )

        if existing_integrity_issue is not None:
            raise ChangeRequestIntegrityBlockedError(
                existing_integrity_issue.id,
                newly_detected=False,
            )

        revision_no = change_request.current_revision

        try:
            revision_submitter_id = (
                get_revision_submitter_id(
                    change_request
                )
            )

        except ChangeRequestApprovalError as exc:
            raise ChangeRequestDenyError(
                "The current revision has invalid submission "
                "history: "
                + str(exc)
            ) from exc

        if revision_submitter_id == approver.pk:
            raise ChangeRequestDenyError(
                "You cannot deny a Change Request revision "
                "that you submitted."
            )

        approval_user_ids = tuple(
            ChangeAction.objects
            .filter(
                change_request=change_request,
                revision_no=revision_no,
                action=ChangeAction.Action.APPROVE,
            )
            .order_by(
                "acted_at",
                "id",
            )
            .values_list(
                "acted_by_id",
                flat=True,
            )
        )

        approval_count = len(
            approval_user_ids
        )

        if approval_count >= 2:
            raise ChangeRequestDenyError(
                "A fully approved Change Request revision "
                "cannot be denied."
            )

        if revision_submitter_id in approval_user_ids:
            raise ChangeRequestDenyError(
                "The Change Request contains an approval "
                "recorded by the revision submitter."
            )

        if approver.pk in approval_user_ids:
            raise ChangeRequestDenyError(
                "You have already approved this Change Request "
                "revision and cannot deny the same revision."
            )

        existing_deny = (
            ChangeAction.objects
            .filter(
                change_request=change_request,
                revision_no=revision_no,
                action=ChangeAction.Action.DENY,
            )
            .exists()
        )

        if existing_deny:
            raise ChangeRequestDenyError(
                "This Change Request revision already contains "
                "a denial action."
            )

        ChangeAction.objects.create(
            change_request=change_request,
            revision_no=revision_no,
            acted_by=approver,
            action=ChangeAction.Action.DENY,
            comment=feedback,
        )

        change_request.status = (
            ChangeRequest.Status.DENIED
        )
        change_request.save(
            update_fields=["status"]
        )

        return StandaloneDenyResult(
            change_request_id=change_request.id,
            status=change_request.status,
            revision_no=revision_no,
            approval_count=approval_count,
        )


def withdraw_standalone_change_request(
        *,
        change_request_id,
        withdrawer,
        comment,
):
    """
    Withdraw a submitted standalone Basic Information Change Request.

    Withdrawal is terminal for the request:

      - the request must be standalone and EDIT_GRANT;
      - the request must currently be PENDING or RETURNED;
      - only the current revision submitter may withdraw it;
      - existing approval history remains immutable;
      - a withdrawal reason is required;
      - one WITHDRAW action is recorded;
      - the Change Request moves to WITHDRAWN;
      - authoritative Form1 values are not changed.
    """
    feedback = (comment or "").strip()

    if (
        withdrawer is None
        or getattr(withdrawer, "pk", None) is None
    ):
        raise ChangeRequestWithdrawError(
            "A saved user is required to withdraw a Change Request."
        )

    if not feedback:
        raise ChangeRequestWithdrawError(
            "Withdrawal requires a reason."
        )

    if len(feedback) > 500:
        raise ChangeRequestWithdrawError(
            "Withdrawal reason cannot exceed 500 characters."
        )

    with transaction.atomic():

        try:
            change_request = (
                ChangeRequest.objects
                .select_for_update()
                .get(
                    pk=change_request_id
                )
            )

        except ChangeRequest.DoesNotExist as exc:
            raise ChangeRequestWithdrawError(
                "The Change Request does not exist."
            ) from exc

        if change_request.coordinated_change_id is not None:
            raise ChangeRequestWithdrawError(
                "A coordinated Change Request cannot be withdrawn "
                "through the standalone workflow."
            )

        if (
            change_request.request_type
            != ChangeRequest.RequestType.EDIT_GRANT
        ):
            raise ChangeRequestWithdrawError(
                "This withdrawal service currently supports only "
                "existing-grant Basic Information Change Requests."
            )

        if change_request.status not in {
            ChangeRequest.Status.PENDING,
            ChangeRequest.Status.RETURNED,
        }:
            raise ChangeRequestWithdrawError(
                "This Change Request is not in a state that can "
                "be withdrawn."
            )

        # -----------------------------------------------------
        # Existing OPEN integrity incidents block normal
        # workflow actions, including withdrawal.
        # -----------------------------------------------------

        existing_integrity_issue = (
            get_open_change_request_integrity_issue(
                change_request,
                lock=True,
            )
        )

        if existing_integrity_issue is not None:
            raise ChangeRequestIntegrityBlockedError(
                existing_integrity_issue.id,
                newly_detected=False,
            )

        revision_no = (
            change_request.current_revision
        )

        # -----------------------------------------------------
        # Only the submitter of the current formal revision
        # may withdraw that revision.
        # -----------------------------------------------------

        try:
            revision_submitter_id = (
                get_revision_submitter_id(
                    change_request
                )
            )

        except ChangeRequestApprovalError as exc:
            raise ChangeRequestWithdrawError(
                "The current revision has invalid submission "
                "history: "
                + str(exc)
            ) from exc

        if revision_submitter_id != withdrawer.pk:
            raise ChangeRequestWithdrawError(
                "Only the submitter of the current Change Request "
                "revision can withdraw it."
            )

        # -----------------------------------------------------
        # Preserve and validate any existing approval history.
        # Withdrawal may occur before Approval #1 or after
        # Approval #1, provided final approval has not occurred.
        # -----------------------------------------------------

        approval_user_ids = tuple(
            ChangeAction.objects
            .filter(
                change_request=change_request,
                revision_no=revision_no,
                action=(
                    ChangeAction.Action.APPROVE
                ),
            )
            .order_by(
                "acted_at",
                "id",
            )
            .values_list(
                "acted_by_id",
                flat=True,
            )
        )

        approval_count = len(
            approval_user_ids
        )

        if approval_count >= 2:
            raise ChangeRequestWithdrawError(
                "A fully approved Change Request revision "
                "cannot be withdrawn."
            )

        if revision_submitter_id in approval_user_ids:
            raise ChangeRequestWithdrawError(
                "The Change Request contains an approval "
                "recorded by the revision submitter."
            )

        # -----------------------------------------------------
        # A current active revision must not already contain
        # a WITHDRAW action.
        # -----------------------------------------------------

        existing_withdraw = (
            ChangeAction.objects
            .filter(
                change_request=change_request,
                revision_no=revision_no,
                action=(
                    ChangeAction.Action.WITHDRAW
                ),
            )
            .exists()
        )

        if existing_withdraw:
            raise ChangeRequestWithdrawError(
                "This Change Request revision already contains "
                "a withdrawal action."
            )

        # -----------------------------------------------------
        # Withdrawal applies no proposed values and performs
        # no GL rematch. It only records the audit event and
        # terminal workflow status.
        # -----------------------------------------------------

        ChangeAction.objects.create(
            change_request=change_request,
            revision_no=revision_no,
            acted_by=withdrawer,
            action=ChangeAction.Action.WITHDRAW,
            comment=feedback,
        )

        change_request.status = (
            ChangeRequest.Status.WITHDRAWN
        )

        change_request.save(
            update_fields=[
                "status",
            ]
        )

        return StandaloneWithdrawResult(
            change_request_id=change_request.id,
            status=change_request.status,
            revision_no=revision_no,
            approval_count=approval_count,
            withdrawn_by_id=withdrawer.pk,
        )


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

    An ordinary returned request validates against the current formal
    revision baseline.

    A request returned through Administrator integrity disposition
    validates against the latest applicable DISPOSITION checkpoint.

    If an authoritative baseline mismatch is detected, the new integrity
    incident is committed first and resubmission is then blocked outside
    the transaction.

    The previous formal revision remains immutable. A successful
    resubmission creates a complete 14-field snapshot, records the actual
    resubmitter, moves the request to the next revision, and starts that
    revision PENDING with zero approvals.
    """
    resubmission_comment = (comment or "").strip()

    if len(resubmission_comment) > 500:
        raise ChangeRequestResubmitError(
            "Resubmission comments cannot exceed 500 characters."
        )

    blocked_integrity_issue_id = None
    blocked_integrity_issue_created = False
    resubmit_result = None

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

            if return_count > 1:
                raise ChangeRequestResubmitError(
                    "The returned revision contains more than one "
                    "Return for Revision action."
                )

            has_closed_integrity_issue = (
                ChangeRequestIntegrityIssue.objects
                .filter(
                    change_request=change_request,
                    revision_no=previous_revision_no,
                    status=(
                        ChangeRequestIntegrityIssue
                        .Status
                        .CLOSED
                    ),
                )
                .exists()
            )

            if (
                not has_closed_integrity_issue
                and return_count != 1
            ):
                raise ChangeRequestResubmitError(
                    "The returned revision does not have a valid "
                    "Return for Revision history."
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

            try:
                baseline_result = (
                    validate_returned_resubmission_baseline(
                        change_request,
                        grant=grant,
                        lock=True,
                    )
                )

            except ChangeRequestBaselineMismatchError:
                detection_result = (
                    detect_or_get_change_request_integrity_issue(
                        change_request_id=change_request.id,
                        detected_by=resubmitter,
                        detected_during=(
                            ChangeRequestIntegrityIssue
                            .DetectedDuring
                            .RESUBMIT
                        ),
                    )
                )

                blocked_integrity_issue_id = (
                    detection_result.integrity_issue_id
                )
                blocked_integrity_issue_created = (
                    detection_result.created
                )

            except ChangeRequestIntegrityBlockedError as exc:
                blocked_integrity_issue_id = (
                    exc.integrity_issue_id
                )
                blocked_integrity_issue_created = False

            except ChangeRequestValidationError as exc:
                raise ChangeRequestResubmitError(
                    str(exc)
                ) from exc

            else:
                if (
                    baseline_result.source
                    == RESUBMISSION_BASELINE_FORMAL_REVISION
                ):
                    if return_count != 1:
                        raise ChangeRequestResubmitError(
                            "An ordinary returned revision must have "
                            "exactly one Return for Revision action."
                        )

                elif (
                    baseline_result.source
                    == RESUBMISSION_BASELINE_INTEGRITY_DISPOSITION
                ):
                    if (
                        baseline_result.integrity_issue_id
                        is None
                    ):
                        raise ChangeRequestResubmitError(
                            "The integrity disposition baseline does not "
                            "identify its integrity incident."
                        )

                else:
                    raise ChangeRequestResubmitError(
                        "The returned revision has an unsupported "
                        "authoritative baseline source."
                    )

                validation_result = (
                    _validate_basic_information_proposed_form_data(
                        change_request=change_request,
                        grant=grant,
                        proposed_form_data=proposed_form_data,
                    )
                )

                _validate_standalone_basic_information_overlap(
                    change_request=change_request,
                    proposed_values=(
                        validation_result.proposed_values
                    ),
                )

                new_revision_no = (
                    previous_revision_no + 1
                )

                if (
                    ChangeRequestField.objects
                    .filter(
                        change_request=change_request,
                        revision_no=new_revision_no,
                    )
                    .exists()
                ):
                    raise ChangeRequestResubmitError(
                        "The next revision already contains field "
                        "snapshots."
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
                        "The next revision already contains workflow "
                        "actions."
                    )

                field_snapshot_values = (
                    build_basic_information_field_snapshot_values(
                        grant=grant,
                        proposed_values=(
                            validation_result.proposed_values
                        ),
                    )
                )

                field_snapshots = [
                    ChangeRequestField(
                        change_request=change_request,
                        revision_no=new_revision_no,
                        field_name=(
                            snapshot["field_name"]
                        ),
                        current_value=(
                            snapshot["current_value"]
                        ),
                        proposed_value=(
                            snapshot["proposed_value"]
                        ),
                    )
                    for snapshot
                    in field_snapshot_values
                ]

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

                resubmit_result = StandaloneResubmitResult(
                    change_request_id=change_request.id,
                    status=change_request.status,
                    previous_revision_no=previous_revision_no,
                    revision_no=new_revision_no,
                    changed_fields=(
                        validation_result.changed_fields
                    ),
                )

    if blocked_integrity_issue_id is not None:
        raise ChangeRequestIntegrityBlockedError(
            blocked_integrity_issue_id,
            newly_detected=(
                blocked_integrity_issue_created
            ),
        )

    return resubmit_result
