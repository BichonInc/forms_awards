from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import TestCase

from .change_request_workflow import (
    _serialize_basic_information_comparison_value,
    build_basic_information_field_snapshot_values,
    serialize_change_request_value,
)
from .forms import (
    GRANT_BASIC_INFORMATION_CHANGE_FIELDS,
    GrantBasicInformationChangeForm,
)
from .models import Form1


class BusinessDateWorkflowRegressionTests(TestCase):
    """
    Protect business-date handling in Basic Information Change Requests.

    Form1 currently stores contract and Internal GL business dates in
    DateTimeField columns, while the web interface intentionally treats
    them as calendar dates.
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
