from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.contrib.auth import get_user_model
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
    Form1,
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
