from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from .change_request_workflow import (
    ChangeRequestValidationError,
    _serialize_basic_information_comparison_value,
    build_basic_information_field_snapshot_values,
    resubmit_standalone_change_request,
    serialize_change_request_value,
)
from .forms import (
    GRANT_BASIC_INFORMATION_CHANGE_FIELDS,
    GrantBasicInformationChangeForm,
)
from .models import (
    ChangeAction,
    ChangeRequest,
    ChangeRequestField,
    CoordinatedChange,
    Form1,
)
from .permissions import (
    ROLE_APPROVER,
    ROLE_EDITOR,
)


class BusinessDateWorkflowRegressionTests(TestCase):
    """
    Protect business-date handling in Basic Information Change Requests.

    Form1 stores contract and Internal GL business dates as DateFields.
    Legacy Change Request audit values may still contain timezone-aware
    DateTimeField-era values, which must continue to compare by calendar
    date.
    """

    def setUp(self):
        self.utc = ZoneInfo("UTC")
        self.pacific = ZoneInfo(
            "America/Los_Angeles"
        )

        self.grant = Form1(
            grant_id="A99999",
            contract_start_date=datetime(
                2021,
                11,
                1,
                0,
                0,
                tzinfo=self.utc,
            ),
            contract_end_date=datetime(
                2024,
                6,
                30,
                0,
                0,
                tzinfo=self.utc,
            ),
            internal_gl_start_date=datetime(
                2021,
                11,
                1,
                0,
                0,
                tzinfo=self.utc,
            ),
            internal_gl_end_date=datetime(
                2024,
                6,
                30,
                0,
                0,
                tzinfo=self.utc,
            ),
        )

    def test_change_form_renders_business_dates_as_html_dates(
            self,
    ):
        initial_values = {
            "contract_start_date": (
                "2021-11-01T00:00:00+00:00"
            ),
            "contract_end_date": (
                "2024-06-30T00:00:00+00:00"
            ),
            "internal_gl_start_date": (
                "2021-11-01T00:00:00+00:00"
            ),
            "internal_gl_end_date": (
                "2024-06-30T00:00:00+00:00"
            ),
        }

        expected_dates = {
            "contract_start_date": "2021-11-01",
            "contract_end_date": "2024-06-30",
            "internal_gl_start_date": "2021-11-01",
            "internal_gl_end_date": "2024-06-30",
        }

        form = GrantBasicInformationChangeForm(
            instance=self.grant,
            initial=initial_values,
            prefix="grant_A99999",
        )

        for field_name, expected_date in (
            expected_dates.items()
        ):
            with self.subTest(
                    field_name=field_name
            ):
                rendered = (
                    form[field_name].as_widget()
                )

                self.assertIn(
                    f'value="{expected_date}"',
                    rendered,
                )

                self.assertNotIn(
                    "T00:00:00",
                    rendered,
                )

    def test_same_business_date_ignores_timezone_difference(
            self,
    ):
        utc_value = datetime(
            2021,
            11,
            1,
            0,
            0,
            tzinfo=self.utc,
        )

        pacific_value = datetime(
            2021,
            11,
            1,
            0,
            0,
            tzinfo=self.pacific,
        )

        self.assertEqual(
            _serialize_basic_information_comparison_value(
                "contract_start_date",
                utc_value,
            ),
            _serialize_basic_information_comparison_value(
                "contract_start_date",
                pacific_value,
            ),
        )

    def test_different_business_date_is_still_a_change(
            self,
    ):
        original_value = datetime(
            2021,
            11,
            1,
            0,
            0,
            tzinfo=self.utc,
        )

        changed_value = datetime(
            2021,
            11,
            2,
            0,
            0,
            tzinfo=self.pacific,
        )

        self.assertNotEqual(
            _serialize_basic_information_comparison_value(
                "contract_start_date",
                original_value,
            ),
            _serialize_basic_information_comparison_value(
                "contract_start_date",
                changed_value,
            ),
        )

    def test_unchanged_business_date_snapshot_preserves_audit_value(
            self,
    ):
        proposed_values = {
            field_name: getattr(
                self.grant,
                field_name,
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

        proposed_values[
            "contract_start_date"
        ] = datetime(
            2021,
            11,
            1,
            0,
            0,
            tzinfo=self.pacific,
        )

        snapshots = (
            build_basic_information_field_snapshot_values(
                grant=self.grant,
                proposed_values=proposed_values,
            )
        )

        snapshots_by_field = {
            snapshot["field_name"]: snapshot
            for snapshot in snapshots
        }

        contract_start_snapshot = (
            snapshots_by_field[
                "contract_start_date"
            ]
        )

        self.assertEqual(
            contract_start_snapshot[
                "current_value"
            ],
            serialize_change_request_value(
                self.grant.contract_start_date
            ),
        )

        self.assertEqual(
            contract_start_snapshot[
                "current_value"
            ],
            "2021-11-01T00:00:00+00:00",
        )

        self.assertIsNone(
            contract_start_snapshot[
                "proposed_value"
            ]
        )

    def test_bound_change_form_preserves_submitted_date_value(
            self,
    ):
        """
        A bound form must preserve the user's submitted date rather than
        replacing it with a normalized value from Form1 or initial data.
        """
        form = GrantBasicInformationChangeForm(
            data={
                (
                    "grant_A99999-"
                    "contract_start_date"
                ): "2021-11-02",
            },
            instance=self.grant,
            initial={
                "contract_start_date": (
                    "2021-11-01T00:00:00+00:00"
                ),
            },
            prefix="grant_A99999",
        )

        rendered = (
            form["contract_start_date"].as_widget()
        )

        self.assertIn(
            'value="2021-11-02"',
            rendered,
        )

        self.assertNotIn(
            'value="2021-11-01"',
            rendered,
        )

    def test_changed_business_date_snapshot_keeps_full_proposed_value(
            self,
    ):
        """
        A real calendar-date change must remain visible in the audit
        snapshot with its complete serialized proposed datetime.
        """
        proposed_values = {
            field_name: getattr(
                self.grant,
                field_name,
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

        proposed_values[
            "contract_start_date"
        ] = datetime(
            2021,
            11,
            2,
            0,
            0,
            tzinfo=self.pacific,
        )

        snapshots = (
            build_basic_information_field_snapshot_values(
                grant=self.grant,
                proposed_values=proposed_values,
            )
        )

        snapshots_by_field = {
            snapshot["field_name"]: snapshot
            for snapshot in snapshots
        }

        contract_start_snapshot = (
            snapshots_by_field[
                "contract_start_date"
            ]
        )

        self.assertEqual(
            contract_start_snapshot[
                "current_value"
            ],
            "2021-11-01T00:00:00+00:00",
        )

        self.assertEqual(
            contract_start_snapshot[
                "proposed_value"
            ],
            "2021-11-02T00:00:00-07:00",
        )

class StandaloneResubmissionRegressionTests(TestCase):
    """
    Protect returned standalone resubmission boundaries.

    A returned standalone request that is revised into a cross-grant
    Internal GL conflict must remain standalone and must not advance to
    a new revision. The Editor is instead guided to withdraw it and
    begin again through Propose Changes as a Coordinated Change Request.
    """

    def setUp(self):
        User = get_user_model()

        self.editor = User.objects.create_user(
            username="editor_test",
            password="test-password",
        )

        self.approver = User.objects.create_user(
            username="approver_test",
            password="test-password",
        )

        self.grant = Form1.objects.create(
            grant_id="A90001",
            program_title="Regression Test Grant",
            contracting_agency="Regression Test Agency",
            contract_number="REG-001",
            amendment_no=0,
            contract_start_date=date(
                2025,
                1,
                1,
            ),
            contract_end_date=date(
                2025,
                12,
                31,
            ),
            contract_amount="100000.00",
            funding_sources=(
                Form1.FundingSource.NONFEDERAL
            ),
            federal_information_status=(
                Form1.FederalInformationStatus
                .NOT_APPLICABLE
            ),
            federal_grantor=None,
            federal_aln=None,
            internal_award_code=900,
            internal_gl_start_date=date(
                2025,
                1,
                1,
            ),
            internal_gl_end_date=date(
                2025,
                1,
                31,
            ),
            status="active",
        )

        self.conflicting_grant = Form1.objects.create(
            grant_id="A90002",
            program_title="Conflicting Regression Grant",
            contracting_agency="Regression Test Agency",
            contract_number="REG-002",
            amendment_no=0,
            contract_start_date=date(
                2025,
                1,
                1,
            ),
            contract_end_date=date(
                2025,
                12,
                31,
            ),
            contract_amount="100000.00",
            funding_sources=(
                Form1.FundingSource.NONFEDERAL
            ),
            federal_information_status=(
                Form1.FederalInformationStatus
                .NOT_APPLICABLE
            ),
            federal_grantor=None,
            federal_aln=None,
            internal_award_code=900,
            internal_gl_start_date=date(
                2025,
                3,
                1,
            ),
            internal_gl_end_date=date(
                2025,
                3,
                31,
            ),
            status="active",
        )

        self.change_request = ChangeRequest.objects.create(
            grant_id=self.grant.grant_id,
            request_type=(
                ChangeRequest.RequestType.EDIT_GRANT
            ),
            status=ChangeRequest.Status.RETURNED,
            current_revision=1,
            submitted_by=self.editor,
            submitted_at=timezone.now(),
        )

        original_proposal = {
            field_name: getattr(
                self.grant,
                field_name,
            )
            for field_name
            in GRANT_BASIC_INFORMATION_CHANGE_FIELDS
        }

        # Revision 1 proposed a legitimate change that did not overlap
        # the second grant.
        original_proposal[
            "internal_gl_end_date"
        ] = date(
            2025,
            2,
            15,
        )

        snapshots = (
            build_basic_information_field_snapshot_values(
                grant=self.grant,
                proposed_values=original_proposal,
            )
        )

        ChangeRequestField.objects.bulk_create(
            [
                ChangeRequestField(
                    change_request=self.change_request,
                    revision_no=1,
                    field_name=snapshot["field_name"],
                    current_value=snapshot["current_value"],
                    proposed_value=snapshot[
                        "proposed_value"
                    ],
                )
                for snapshot
                in snapshots
            ]
        )

        ChangeAction.objects.create(
            change_request=self.change_request,
            revision_no=1,
            acted_by=self.approver,
            action=ChangeAction.Action.RETURN,
            comment="Please revise the GL assignment.",
        )

    def test_returned_standalone_gl_conflict_blocks_resubmission(
            self,
    ):
        proposed_form_data = {
            "program_title": self.grant.program_title,
            "contracting_agency": (
                self.grant.contracting_agency
            ),
            "contract_number": self.grant.contract_number,
            "amendment_no": str(
                self.grant.amendment_no
            ),
            "contract_start_date": (
                self.grant.contract_start_date.isoformat()
            ),
            "contract_end_date": (
                self.grant.contract_end_date.isoformat()
            ),
            "contract_amount": str(
                self.grant.contract_amount
            ),
            "funding_sources": (
                Form1.FundingSource.NONFEDERAL
            ),
            "federal_information_status": (
                Form1.FederalInformationStatus
                .NOT_APPLICABLE
            ),
            "federal_grantor": "",
            "federal_aln": "",
            "internal_award_code": "900",
            "internal_gl_start_date": "2025-01-01",
            "internal_gl_end_date": "2025-03-15",
        }

        revision_one_snapshot_count = (
            ChangeRequestField.objects
            .filter(
                change_request=self.change_request,
                revision_no=1,
            )
            .count()
        )

        with self.assertRaises(
                ChangeRequestValidationError
        ) as error_context:
            resubmit_standalone_change_request(
                change_request_id=(
                    self.change_request.id
                ),
                resubmitter=self.editor,
                proposed_form_data=(
                    proposed_form_data
                ),
                comment="Revised GL dates.",
            )

        error_message = str(
            error_context.exception
        )

        self.assertIn(
            "A90002",
            error_message,
        )
        self.assertIn(
            "return to Review",
            error_message,
        )
        self.assertIn(
            "withdraw this Change Request",
            error_message,
        )
        self.assertIn(
            "Propose Changes",
            error_message,
        )
        self.assertIn(
            "Coordinated Change Request",
            error_message,
        )

        self.change_request.refresh_from_db()
        self.grant.refresh_from_db()

        self.assertEqual(
            self.change_request.status,
            ChangeRequest.Status.RETURNED,
        )
        self.assertEqual(
            self.change_request.current_revision,
            1,
        )

        self.assertEqual(
            ChangeRequestField.objects.filter(
                change_request=self.change_request,
                revision_no=1,
            ).count(),
            revision_one_snapshot_count,
        )

        self.assertFalse(
            ChangeRequestField.objects.filter(
                change_request=self.change_request,
                revision_no=2,
            ).exists()
        )

        self.assertFalse(
            ChangeAction.objects.filter(
                change_request=self.change_request,
                revision_no=2,
                action=ChangeAction.Action.RESUBMIT,
            ).exists()
        )

        self.assertEqual(
            self.grant.internal_gl_start_date,
            date(
                2025,
                1,
                1,
            ),
        )
        self.assertEqual(
            self.grant.internal_gl_end_date,
            date(
                2025,
                1,
                31,
            ),
        )

class ActionQueueRegressionTests(TestCase):
    """
    Protect the personalized New Grant and Existing Grant
    action queues.

    Queue counts and queue pages must represent work the
    current user can still act on, rather than every active
    Change Request in the system.
    """

    def setUp(self):
        User = get_user_model()

        self.editor = User.objects.create_user(
            username="queue_editor",
            password="test-password",
        )

        self.approver_one = User.objects.create_user(
            username="queue_approver_one",
            password="test-password",
        )

        self.approver_two = User.objects.create_user(
            username="queue_approver_two",
            password="test-password",
        )

        self.dual_role_user = User.objects.create_user(
            username="queue_editor_approver",
            password="test-password",
        )

        editor_group, _ = Group.objects.get_or_create(
            name=ROLE_EDITOR,
        )

        approver_group, _ = Group.objects.get_or_create(
            name=ROLE_APPROVER,
        )

        self.editor.groups.add(
            editor_group,
        )

        self.approver_one.groups.add(
            approver_group,
        )

        self.approver_two.groups.add(
            approver_group,
        )

        self.dual_role_user.groups.add(
            editor_group,
            approver_group,
        )

        self.existing_grant = Form1.objects.create(
            grant_id="A91001",
            program_title="Queue Regression Grant",
            contracting_agency="Queue Regression Agency",
            contract_number="QUEUE-EXISTING-001",
            amendment_no=0,
            contract_start_date=date(
                2025,
                1,
                1,
            ),
            contract_end_date=date(
                2025,
                12,
                31,
            ),
            contract_amount="100000.00",
            funding_sources=(
                Form1.FundingSource.NONFEDERAL
            ),
            federal_information_status=(
                Form1.FederalInformationStatus
                .NOT_APPLICABLE
            ),
            internal_award_code=910,
            internal_gl_start_date=date(
                2025,
                1,
                1,
            ),
            internal_gl_end_date=date(
                2025,
                12,
                31,
            ),
        )

    def _create_change_request(
            self,
            *,
            grant_id,
            request_type,
            status,
            submitter=None,
            coordinated_change=None,
    ):
        if submitter is None:
            submitter = self.editor

        return ChangeRequest.objects.create(
            grant_id=grant_id,
            request_type=request_type,
            status=status,
            coordinated_change=coordinated_change,
            current_revision=1,
            submitted_by=submitter,
            submitted_at=timezone.now(),
        )

    def _approve(
            self,
            change_request,
            approver,
    ):
        return ChangeAction.objects.create(
            change_request=change_request,
            revision_no=change_request.current_revision,
            acted_by=approver,
            action=ChangeAction.Action.APPROVE,
        )

    def _grant_list_context_for(self, user):
        self.client.force_login(user)

        response = self.client.get(
            reverse("grant_list"),
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        return response.context

    def test_pending_existing_request_disappears_after_user_approval(
            self,
    ):
        change_request = self._create_change_request(
            grant_id=self.existing_grant.grant_id,
            request_type=(
                ChangeRequest.RequestType.EDIT_GRANT
            ),
            status=ChangeRequest.Status.PENDING,
        )

        self.client.force_login(
            self.approver_one
        )

        response = self.client.get(
            reverse("existing_grant_requests"),
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            len(response.context["standalone_rows"]),
            1,
        )

        context = self._grant_list_context_for(
            self.approver_one
        )

        self.assertEqual(
            context[
                "active_existing_grant_request_count"
            ],
            1,
        )

        self._approve(
            change_request,
            self.approver_one,
        )

        self.client.force_login(
            self.approver_one
        )

        response = self.client.get(
            reverse("existing_grant_requests"),
        )

        self.assertEqual(
            len(response.context["standalone_rows"]),
            0,
        )

        context = self._grant_list_context_for(
            self.approver_one
        )

        self.assertEqual(
            context[
                "active_existing_grant_request_count"
            ],
            0,
        )

        self.client.force_login(
            self.approver_two
        )

        response = self.client.get(
            reverse("existing_grant_requests"),
        )

        self.assertEqual(
            len(response.context["standalone_rows"]),
            1,
        )

    def test_returned_existing_request_is_editor_work(
            self,
    ):
        self._create_change_request(
            grant_id=self.existing_grant.grant_id,
            request_type=(
                ChangeRequest.RequestType.EDIT_GRANT
            ),
            status=ChangeRequest.Status.RETURNED,
        )

        self.client.force_login(
            self.editor
        )

        response = self.client.get(
            reverse("existing_grant_requests"),
        )

        self.assertEqual(
            len(response.context["standalone_rows"]),
            1,
        )

        context = self._grant_list_context_for(
            self.editor
        )

        self.assertEqual(
            context[
                "active_existing_grant_request_count"
            ],
            1,
        )

        self.client.force_login(
            self.approver_one
        )

        response = self.client.get(
            reverse("existing_grant_requests"),
        )

        self.assertEqual(
            len(response.context["standalone_rows"]),
            0,
        )

        context = self._grant_list_context_for(
            self.approver_one
        )

        self.assertEqual(
            context[
                "active_existing_grant_request_count"
            ],
            0,
        )

    def test_pending_new_grant_disappears_after_user_approval(
            self,
    ):
        change_request = self._create_change_request(
            grant_id="A91991",
            request_type=(
                ChangeRequest.RequestType.NEW_GRANT
            ),
            status=ChangeRequest.Status.PENDING,
        )

        self.client.force_login(
            self.approver_one
        )

        response = self.client.get(
            reverse("new_grant_requests"),
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            len(response.context["request_rows"]),
            1,
        )

        context = self._grant_list_context_for(
            self.approver_one
        )

        self.assertEqual(
            context[
                "active_new_grant_request_count"
            ],
            1,
        )

        self._approve(
            change_request,
            self.approver_one,
        )

        self.client.force_login(
            self.approver_one
        )

        response = self.client.get(
            reverse("new_grant_requests"),
        )

        self.assertEqual(
            len(response.context["request_rows"]),
            0,
        )

        context = self._grant_list_context_for(
            self.approver_one
        )

        self.assertEqual(
            context[
                "active_new_grant_request_count"
            ],
            0,
        )

        self.client.force_login(
            self.approver_two
        )

        response = self.client.get(
            reverse("new_grant_requests"),
        )

        self.assertEqual(
            len(response.context["request_rows"]),
            1,
        )

        context = self._grant_list_context_for(
            self.approver_two
        )

        self.assertEqual(
            context[
                "active_new_grant_request_count"
            ],
            1,
        )

    def test_returned_new_grant_is_editor_work(
            self,
    ):
        self._create_change_request(
            grant_id="A91992",
            request_type=(
                ChangeRequest.RequestType.NEW_GRANT
            ),
            status=ChangeRequest.Status.RETURNED,
        )

        self.client.force_login(
            self.editor
        )

        response = self.client.get(
            reverse("new_grant_requests"),
        )

        self.assertEqual(
            len(response.context["request_rows"]),
            1,
        )

        context = self._grant_list_context_for(
            self.editor
        )

        self.assertEqual(
            context[
                "active_new_grant_request_count"
            ],
            1,
        )

        self.client.force_login(
            self.approver_one
        )

        response = self.client.get(
            reverse("new_grant_requests"),
        )

        self.assertEqual(
            len(response.context["request_rows"]),
            0,
        )

        context = self._grant_list_context_for(
            self.approver_one
        )

        self.assertEqual(
            context[
                "active_new_grant_request_count"
            ],
            0,
        )

    def test_coordinated_package_counts_once_and_is_personalized(
            self,
    ):
        coordinated_change = (
            CoordinatedChange.objects.create(
                status=CoordinatedChange.Status.PENDING,
                created_by=self.editor,
                submitted_by=self.editor,
                submitted_at=timezone.now(),
                current_revision=1,
            )
        )

        child_one = self._create_change_request(
            grant_id="A91993",
            request_type=(
                ChangeRequest.RequestType.EDIT_GRANT
            ),
            status=ChangeRequest.Status.PENDING,
            coordinated_change=coordinated_change,
        )

        child_two = self._create_change_request(
            grant_id="A91994",
            request_type=(
                ChangeRequest.RequestType.EDIT_GRANT
            ),
            status=ChangeRequest.Status.PENDING,
            coordinated_change=coordinated_change,
        )

        self.client.force_login(
            self.approver_one
        )

        response = self.client.get(
            reverse("existing_grant_requests"),
        )

        self.assertEqual(
            len(response.context["coordinated_rows"]),
            1,
        )

        context = self._grant_list_context_for(
            self.approver_one
        )

        self.assertEqual(
            context[
                "active_existing_grant_request_count"
            ],
            1,
        )

        self._approve(
            child_one,
            self.approver_one,
        )

        self._approve(
            child_two,
            self.approver_one,
        )

        self.client.force_login(
            self.approver_one
        )

        response = self.client.get(
            reverse("existing_grant_requests"),
        )

        self.assertEqual(
            len(response.context["coordinated_rows"]),
            0,
        )

        context = self._grant_list_context_for(
            self.approver_one
        )

        self.assertEqual(
            context[
                "active_existing_grant_request_count"
            ],
            0,
        )

        self.client.force_login(
            self.approver_two
        )

        response = self.client.get(
            reverse("existing_grant_requests"),
        )

        self.assertEqual(
            len(response.context["coordinated_rows"]),
            1,
        )

        row = response.context[
            "coordinated_rows"
        ][0]

        self.assertEqual(
            row["grant_ids"],
            [
                "A91993",
                "A91994",
            ],
        )

        self.assertEqual(
            row["approval_label"],
            "1 of 2",
        )

        context = self._grant_list_context_for(
            self.approver_two
        )

        self.assertEqual(
            context[
                "active_existing_grant_request_count"
            ],
            1,
        )

    def test_dual_role_submitter_does_not_see_own_pending_request(
            self,
    ):
        self._create_change_request(
            grant_id="A91995",
            request_type=(
                ChangeRequest.RequestType.NEW_GRANT
            ),
            status=ChangeRequest.Status.PENDING,
            submitter=self.dual_role_user,
        )

        self.client.force_login(
            self.dual_role_user
        )

        response = self.client.get(
            reverse("new_grant_requests"),
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            len(response.context["request_rows"]),
            0,
        )

        context = self._grant_list_context_for(
            self.dual_role_user
        )

        self.assertEqual(
            context[
                "active_new_grant_request_count"
            ],
            0,
        )

class CsvExportRegressionTests(TestCase):
    """
    Protect CSV export classification, allocation, and Fiscal Review
    behavior.

    The CSV must use the same current Funding Sources logic as Grant
    Detail rather than relying on legacy stored allocation values.
    """

    def setUp(self):
        User = get_user_model()

        self.user = User.objects.create_user(
            username="csv_regression_user",
            password="test-password",
        )

        self.client.force_login(
            self.user
        )

    def _create_grant(
            self,
            *,
            grant_id,
            award_code,
            funding_sources,
            federal_information_status,
            federal_grantor=None,
            federal_aln=None,
    ):
        return Form1.objects.create(
            grant_id=grant_id,
            program_title="CSV Regression Program",
            contracting_agency="CSV Regression Agency",
            contract_number=f"CSV-{grant_id}",
            amendment_no=0,
            contract_start_date=date(
                2025,
                1,
                1,
            ),
            contract_end_date=date(
                2025,
                12,
                31,
            ),
            contract_amount="100000.00",
            funding_sources=funding_sources,
            federal_information_status=(
                federal_information_status
            ),
            federal_grantor=federal_grantor,
            federal_aln=federal_aln,
            internal_award_code=award_code,
            internal_gl_start_date=date(
                2025,
                1,
                1,
            ),
            internal_gl_end_date=date(
                2025,
                12,
                31,
            ),
        )

    def _create_gl_expenditure(
            self,
            *,
            grant,
            amount,
            fiscal_year="FY24-25",
    ):
        from .models import GLExpenditure

        grant_field = (
            GLExpenditure
            ._meta
            .get_field("grant_id")
        )

        kwargs = {
            "effective_date": date(
                2025,
                6,
                30,
            ),
            "award_code": str(
                grant.internal_award_code
            ),
            "debit": "0.00",
            "credit": str(amount),
            "net_expenditure": str(amount),
            "fiscal_year": fiscal_year,
        }

        if grant_field.is_relation:
            kwargs["grant_id"] = grant
        else:
            kwargs["grant_id"] = grant.grant_id

        return GLExpenditure.objects.create(
            **kwargs
        )

    def _download_rows(self):
        import csv
        from io import StringIO

        response = self.client.get(
            reverse("download_data_csv"),
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        text = response.content.decode(
            "utf-8"
        )

        reader = csv.DictReader(
            StringIO(text)
        )

        return (
            reader.fieldnames,
            list(reader),
        )

    def _get_gl_row(
            self,
            rows,
            grant_id,
            fiscal_year="FY24-25",
    ):
        matches = [
            row
            for row in rows
            if (
                row["Grant ID"] == grant_id
                and row["Source"]
                == "GL Expenditure"
                and row["Fiscal Year"]
                == fiscal_year
            )
        ]

        self.assertEqual(
            len(matches),
            1,
        )

        return matches[0]

    def test_csv_uses_current_headers(self):
        self._create_grant(
            grant_id="A92001",
            award_code=920,
            funding_sources=(
                Form1.FundingSource.NONFEDERAL
            ),
            federal_information_status=(
                Form1.FederalInformationStatus
                .NOT_APPLICABLE
            ),
        )

        fieldnames, _ = (
            self._download_rows()
        )

        self.assertIn(
            "Funding Sources",
            fieldnames,
        )

        self.assertIn(
            "Federal Information Status",
            fieldnames,
        )

        self.assertIn(
            "Fiscal Review",
            fieldnames,
        )

        self.assertIn(
            "Fiscal Review Reasons",
            fieldnames,
        )

        self.assertNotIn(
            "Difference",
            fieldnames,
        )

        self.assertNotIn(
            "Fiscal Review Issues",
            fieldnames,
        )

    def test_federal_to_nonfederal_reallocates_current_gl_total(
            self,
    ):
        from .models import GLExpenditure

        grant = self._create_grant(
            grant_id="A92002",
            award_code=921,
            funding_sources=(
                Form1.FundingSource.FEDERAL
            ),
            federal_information_status=(
                Form1.FederalInformationStatus
                .COMPLETE
            ),
            federal_grantor="Federal Agency",
            federal_aln="93.001",
        )

        gl_row = self._create_gl_expenditure(
            grant=grant,
            amount="123.45",
        )

        _, rows = self._download_rows()

        csv_row = self._get_gl_row(
            rows,
            grant.grant_id,
        )

        self.assertEqual(
            csv_row["Total Expenditure"],
            "123.45",
        )

        self.assertEqual(
            csv_row["Federal"],
            "123.45",
        )

        self.assertEqual(
            csv_row["Nonfederal"],
            "0.00",
        )

        original_gl_id = gl_row.pk

        grant.funding_sources = (
            Form1.FundingSource.NONFEDERAL
        )

        grant.federal_information_status = (
            Form1.FederalInformationStatus
            .NOT_APPLICABLE
        )

        grant.federal_grantor = None
        grant.federal_aln = None

        grant.save(
            update_fields=[
                "funding_sources",
                "federal_information_status",
                "federal_grantor",
                "federal_aln",
            ]
        )

        _, rows = self._download_rows()

        csv_row = self._get_gl_row(
            rows,
            grant.grant_id,
        )

        self.assertEqual(
            csv_row["Federal"],
            "0.00",
        )

        self.assertEqual(
            csv_row["Nonfederal"],
            "123.45",
        )

        # Funding classification does not alter GL ownership.
        self.assertTrue(
            GLExpenditure.objects.filter(
                pk=original_gl_id,
                grant_id=grant.grant_id,
            ).exists()
        )

    def test_csv_exports_classification_review_labels(
            self,
    ):
        funding_review_grant = (
            self._create_grant(
                grant_id="A92003",
                award_code=922,
                funding_sources=(
                    Form1.FundingSource
                    .REVIEW_REQUIRED
                ),
                federal_information_status=(
                    Form1.FederalInformationStatus
                    .COMPLETE
                ),
                federal_grantor="Federal Agency",
                federal_aln="93.002",
            )
        )

        federal_info_grant = (
            self._create_grant(
                grant_id="A92004",
                award_code=923,
                funding_sources=(
                    Form1.FundingSource.FEDERAL
                ),
                federal_information_status=(
                    Form1.FederalInformationStatus
                    .PENDING
                ),
            )
        )

        _, rows = self._download_rows()

        funding_rows = [
            row
            for row in rows
            if (
                row["Grant ID"]
                == funding_review_grant.grant_id
            )
        ]

        self.assertTrue(
            funding_rows
        )

        for row in funding_rows:
            self.assertEqual(
                row["Funding Sources"],
                "Needs Review",
            )

            self.assertEqual(
                row["Fiscal Review"],
                "1",
            )

            self.assertIn(
                "Funding Sources Needs Review",
                row["Fiscal Review Reasons"],
            )

        federal_info_rows = [
            row
            for row in rows
            if (
                row["Grant ID"]
                == federal_info_grant.grant_id
            )
        ]

        self.assertTrue(
            federal_info_rows
        )

        for row in federal_info_rows:
            self.assertEqual(
                row[
                    "Federal Information Status"
                ],
                "Federal Information Incomplete",
            )

            self.assertEqual(
                row["Fiscal Review"],
                "1",
            )

            self.assertIn(
                "Federal Information Incomplete",
                row["Fiscal Review Reasons"],
            )

    def test_both_stale_allocation_is_recalculated_and_flagged(
            self,
    ):
        from .models import FiscalBreakdown

        grant = self._create_grant(
            grant_id="A92005",
            award_code=924,
            funding_sources=(
                Form1.FundingSource.BOTH
            ),
            federal_information_status=(
                Form1.FederalInformationStatus
                .COMPLETE
            ),
            federal_grantor="Federal Agency",
            federal_aln="93.003",
        )

        self._create_gl_expenditure(
            grant=grant,
            amount="120.00",
        )

        FiscalBreakdown.objects.create(
            grant_id=grant,
            fiscal_year="FY24-25",
            federal="40.00",
            nonfederal="60.00",
            reviewed_total_allowed_expenditure=(
                "100.00"
            ),
        )

        _, rows = self._download_rows()

        csv_row = self._get_gl_row(
            rows,
            grant.grant_id,
        )

        self.assertEqual(
            csv_row["Total Expenditure"],
            "120.00",
        )

        self.assertEqual(
            csv_row["Federal"],
            "40.00",
        )

        self.assertEqual(
            csv_row["Nonfederal"],
            "80.00",
        )

        self.assertEqual(
            csv_row["Fiscal Review"],
            "1",
        )

        self.assertIn(
            "In-Period Allocation Needs Review",
            csv_row["Fiscal Review Reasons"],
        )

class FundingSourceTransitionRegressionTests(TestCase):
    """
    Protect allocation-review behavior when an approved existing-grant
    Basic Information change moves a grant into Both funding.
    """

    def test_entering_both_invalidates_review_snapshots_but_preserves_allocations(
            self,
    ):
        from .change_request_workflow import (
            BasicInformationValidationResult,
            _apply_validated_basic_information_values,
        )
        from .models import (
            FiscalBreakdown,
            SubsequentFiscalBreakdown,
        )

        grant = Form1.objects.create(
            grant_id="A93001",
            program_title="Funding Transition Test",
            contracting_agency="Regression Agency",
            contract_number="TRANSITION-001",
            amendment_no=0,
            contract_start_date=date(
                2025,
                1,
                1,
            ),
            contract_end_date=date(
                2025,
                12,
                31,
            ),
            contract_amount="100000.00",
            funding_sources=(
                Form1.FundingSource.FEDERAL
            ),
            federal_information_status=(
                Form1.FederalInformationStatus
                .COMPLETE
            ),
            federal_grantor="Federal Agency",
            federal_aln="93.010",
            internal_award_code="930",
            internal_gl_start_date=date(
                2025,
                1,
                1,
            ),
            internal_gl_end_date=date(
                2025,
                12,
                31,
            ),
        )

        in_period = FiscalBreakdown.objects.create(
            grant_id=grant,
            fiscal_year="FY24-25",
            federal="70.00",
            nonfederal="30.00",
            reviewed_total_allowed_expenditure=(
                "100.00"
            ),
        )

        subsequent = (
            SubsequentFiscalBreakdown.objects.create(
                grant_id=grant,
                fiscal_year="FY24-25",
                federal="7.00",
                nonfederal="3.00",
                reviewed_total_subsequent_adjustment=(
                    "10.00"
                ),
            )
        )

        validation_result = (
            BasicInformationValidationResult(
                proposed_values={
                    "funding_sources": (
                        Form1.FundingSource.BOTH
                    ),
                },
                changed_fields=(
                    "funding_sources",
                ),
                gl_rematch_required=False,
            )
        )

        gl_rematch_result = (
            _apply_validated_basic_information_values(
                grant=grant,
                validation_result=validation_result,
            )
        )

        grant.refresh_from_db()
        in_period.refresh_from_db()
        subsequent.refresh_from_db()

        self.assertIsNone(
            gl_rematch_result
        )

        self.assertEqual(
            grant.funding_sources,
            Form1.FundingSource.BOTH,
        )

        # Preserve historical/manual allocation amounts.
        self.assertEqual(
            in_period.federal,
            Decimal("70.00"),
        )
        self.assertEqual(
            in_period.nonfederal,
            Decimal("30.00"),
        )
        self.assertEqual(
            subsequent.federal,
            Decimal("7.00"),
        )
        self.assertEqual(
            subsequent.nonfederal,
            Decimal("3.00"),
        )

        # But require the Accountant to review those allocations again.
        self.assertIsNone(
            in_period.reviewed_total_allowed_expenditure
        )
        self.assertIsNone(
            subsequent.reviewed_total_subsequent_adjustment
        )
