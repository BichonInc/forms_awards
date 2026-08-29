import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models

GRANT_ID_VALIDATOR = RegexValidator(
    regex=r"^[A-Z]\d{5}$",
    message=(
        "Grant ID must contain one capital letter "
        "followed by five digits."
    ),
)

class Form1(models.Model):
    class FederalInformationStatus(models.TextChoices):
        NOT_APPLICABLE = (
            "NOT_APPLICABLE",
            "Not Applicable — No Federal Funding",
        )
        PENDING = (
            "PENDING",
            "Pending Federal Information",
        )
        COMPLETE = (
            "COMPLETE",
            "Federal Information Complete",
        )
        NO_ALN = (
            "NO_ALN",
            "Federal Funding with No ALN",
        )

    class FundingSource(models.TextChoices):
        FEDERAL = "FEDERAL", "100% Federal"
        NONFEDERAL = "NONFEDERAL", "100% Non-federal"
        BOTH = "BOTH", "Both Federal and Non-federal"
        REVIEW_REQUIRED = (
            "REVIEW_REQUIRED",
            "Needs Review — Funding Source Undetermined",
        )

    class ProgramIncomeTreatment(models.TextChoices):
        NOT_RESEARCHED = (
            "NOT_RESEARCHED",
            "Not Researched",
        )
        DEDUCTIVE = (
            "DEDUCTIVE",
            "Deductive",
        )
        ADDITIVE = (
            "ADDITIVE",
            "Additive",
        )
        COST_SHARING = (
            "COST_SHARING",
            "Cost Sharing / Matching",
        )
        OTHER = (
            "OTHER",
            "Other / Contract-Specific Treatment",
        )

    grant_id = models.CharField(
        max_length=10,
        primary_key=True,
        validators=[GRANT_ID_VALIDATOR],
    )
    program_title = models.CharField(max_length=255)
    contracting_agency = models.CharField(max_length=255)
    contract_number = models.CharField(max_length=50, null=False)
    amendment_no = models.PositiveSmallIntegerField(
        default=0,
        validators=[
            MinValueValidator(0),
            MaxValueValidator(99),
        ],
    )
    contract_start_date = models.DateTimeField(null=True, blank=True)
    contract_end_date = models.DateTimeField(null=True, blank=True)
    contract_amount = models.DecimalField(max_digits=10, decimal_places=2)
    program_income_treatment = models.CharField(
        max_length=20,
        choices=ProgramIncomeTreatment.choices,
        default=ProgramIncomeTreatment.NOT_RESEARCHED,
    )
    federal_funding_included = models.BooleanField(
        null=True,
        blank=True,
    )
    funding_sources = models.CharField(
        max_length=20,
        choices=FundingSource.choices,
        null=True,
        blank=True,
    )
    federal_information_status = models.CharField(
        max_length=20,
        choices=FederalInformationStatus.choices,
        null=True,
        blank=True,
    )
    federal_grantor = models.CharField(max_length=255, null=True, blank=True)
    federal_aln = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        validators=[
            RegexValidator(
                regex=r'^\d{2}\.\d{3}$',
                message="Federal ALN must be in the format 'xx.xxx', where x is a digit."
            )
        ]
    )
    internal_award_code = models.CharField(max_length=10)
    def clean(self):
        super().clean()
        if self.internal_award_code:
            try:
                internal_award_code_int = int(self.internal_award_code)
            except ValueError:
                raise ValidationError("Internal Award Code must be a numeric value.")

            if internal_award_code_int < 100 or internal_award_code_int > 999:
                raise ValidationError("Internal Award Code must be between 100 and 999.")

    internal_gl_start_date = models.DateTimeField(null=True, blank=True)
    internal_gl_end_date = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=10,
        choices=[('active', 'Active'), ('inactive', 'Inactive')]
    )

    class Meta:
        db_table = 'form_1'


class GLExpenditure(models.Model):
    effective_date = models.DateField()
    award_code = models.CharField(max_length=10)
    debit = models.DecimalField(max_digits=10, decimal_places=2)
    credit = models.DecimalField(max_digits=10, decimal_places=2)
    net_expenditure = models.DecimalField(max_digits=10, decimal_places=2)
    fiscal_year = models.CharField(max_length=7)
    grant_id = models.CharField(max_length=10, null=True, blank=True)  # Allow null values

    class Meta:
        db_table = 'gl_expenditure'  # Use the existing table name


class FiscalBreakdown(models.Model):
    grant_id = models.ForeignKey(
        Form1,
        on_delete=models.CASCADE,
        db_column='grant_id',
    )
    fiscal_year = models.CharField(max_length=7)
    federal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    nonfederal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    reviewed_total_allowed_expenditure = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    class Meta:
        db_table = 'fiscal_breakdown'  # Custom table name


class SubsequentAdjustment(models.Model):
    effective_date = models.DateField()
    award_code = models.CharField(max_length=10)
    debit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    credit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    net_expenditure = models.DecimalField(max_digits=10, decimal_places=2)
    fiscal_year = models.CharField(max_length=10)
    grant_id = models.ForeignKey(Form1, null=True, blank=True, on_delete=models.SET_NULL, db_column='grant_id')


class SubsequentFiscalBreakdown(models.Model):
    grant_id = models.ForeignKey(
        Form1,
        on_delete=models.CASCADE,
        db_column='grant_id',
    )
    fiscal_year = models.CharField(max_length=10)
    federal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
    )
    nonfederal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
    )
    reviewed_total_subsequent_adjustment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )


class ProgramIncome(models.Model):
    grant = models.ForeignKey(
        Form1,
        on_delete=models.CASCADE,
        db_column="grant_id",
        related_name="program_income_entries",
    )
    fiscal_year = models.CharField(
        max_length=10,
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    class Meta:
        db_table = "program_income"
        constraints = [
            models.UniqueConstraint(
                fields=["grant", "fiscal_year"],
                name="unique_program_income_grant_fiscal_year",
            )
        ]

    def __str__(self):
        return (
            f"{self.grant_id} "
            f"{self.fiscal_year}: "
            f"{self.amount}"
        )


class GrantFiscalExceptionReview(models.Model):
    class ExceptionType(models.TextChoices):
        NEGATIVE_CONTRACT_BALANCE = (
            "NEGATIVE_CONTRACT_BALANCE",
            "Negative Contract Balance",
        )

    grant = models.ForeignKey(
        Form1,
        on_delete=models.PROTECT,
        db_column="grant_id",
        related_name="fiscal_exception_reviews",
    )
    exception_type = models.CharField(
        max_length=40,
        choices=ExceptionType.choices,
        default=ExceptionType.NEGATIVE_CONTRACT_BALANCE,
    )
    reviewed_contract_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    reviewed_total_allowed_expenditure = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    reviewed_program_income = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    reviewed_program_income_treatment = models.CharField(
        max_length=20,
        choices=Form1.ProgramIncomeTreatment.choices,
    )
    reviewed_contract_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    explanation = models.TextField()
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="accepted_fiscal_exception_reviews",
    )
    accepted_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        db_table = "grant_fiscal_exception_review"
        ordering = [
            "-accepted_at",
            "-id",
        ]

    def __str__(self):
        accepted_date = (
            self.accepted_at.strftime("%Y-%m-%d")
            if self.accepted_at
            else "not yet accepted"
        )
        return (
            f"{self.grant_id}: "
            f"{self.get_exception_type_display()} "
            f"accepted {accepted_date}"
        )


class CoordinatedChange(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING = "PENDING", "Pending"
        RETURNED = "RETURNED", "Returned for Revision"
        APPLIED = "APPLIED", "Applied"
        DENIED = "DENIED", "Denied"

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="coordinated_changes_created",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    applied_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "coordinated_change"

    def __str__(self):
        return (
            f"Coordinated Change {self.id} "
            f"({self.get_status_display()})"
        )


class ChangeRequest(models.Model):
    class RequestType(models.TextChoices):
        NEW_GRANT = "NEW_GRANT", "New Grant"
        EDIT_GRANT = "EDIT_GRANT", "Edit Grant"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING = "PENDING", "Pending"
        RETURNED = "RETURNED", "Returned for Revision"
        READY = "READY", "Approved - Waiting to Apply"
        DENIED = "DENIED", "Denied"
        APPROVED = "APPROVED", "Approved"

    grant_id = models.CharField(
        max_length=10,
        validators=[GRANT_ID_VALIDATOR],
    )
    request_type = models.CharField(
        max_length=20,
        choices=RequestType.choices,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    coordinated_change = models.ForeignKey(
        CoordinatedChange,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="change_requests",
    )
    current_revision = models.PositiveIntegerField(default=1)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submitted_change_requests",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "change_request"
        constraints = [
            models.UniqueConstraint(
                fields=["grant_id"],
                condition=models.Q(
                    status__in=[
                        "DRAFT",
                        "PENDING",
                        "RETURNED",
                        "READY",
                    ]
                ),
                name="unique_active_change_request_per_grant",
            )
        ]

    def __str__(self):
        return (
            f"Request {self.id}: {self.grant_id} "
            f"({self.get_request_type_display()})"
        )


class ChangeRequestField(models.Model):
    change_request = models.ForeignKey(
        ChangeRequest,
        on_delete=models.PROTECT,
        related_name="field_changes",
    )
    field_name = models.CharField(max_length=100)
    revision_no = models.PositiveIntegerField(default=1)
    current_value = models.TextField(null=True, blank=True)
    proposed_value = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "change_request_field"
        constraints = [
            models.UniqueConstraint(
                fields=["change_request", "revision_no", "field_name"],
                name="unique_field_per_request_revision",
            )
        ]

    def __str__(self):
        return f"{self.change_request_id}: {self.field_name}"


class ChangeAction(models.Model):
    class Action(models.TextChoices):
        APPROVE = "APPROVE", "Approve"
        RETURN = "RETURN", "Return for Revision"
        DENY = "DENY", "Deny"
        RESUBMIT = "RESUBMIT", "Resubmit"

    change_request = models.ForeignKey(
        ChangeRequest,
        on_delete=models.PROTECT,
        related_name="actions",
    )
    revision_no = models.PositiveIntegerField(default=1)
    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="change_actions",
    )
    action = models.CharField(
        max_length=10,
        choices=Action.choices,
    )
    acted_at = models.DateTimeField(auto_now_add=True)
    comment = models.TextField(
        blank=True,
        default="",
        max_length=500,
    )

    class Meta:
        db_table = "change_action"
        ordering = ["acted_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "change_request",
                    "revision_no",
                    "acted_by",
                ],
                condition=models.Q(action="APPROVE"),
                name="unique_approval_per_user_per_revision",
            )
        ]

    def __str__(self):
        return (
            f"Request {self.change_request_id}: "
            f"{self.action} by {self.acted_by}"
        )


class ChangeNote(models.Model):
    change_request = models.ForeignKey(
        ChangeRequest,
        on_delete=models.PROTECT,
        related_name="notes",
    )
    revision_no = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="change_notes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    note_text = models.TextField(max_length=1000)

    class Meta:
        db_table = "change_note"
        ordering = ["created_at", "id"]

    def __str__(self):
        preview = self.note_text[:50]
        return (
            f"Request {self.change_request_id}, "
            f"revision {self.revision_no}: {preview}"
        )


def grant_document_upload_path(instance, filename):
    return (
        f"grant_documents/{instance.grant_id}/"
        f"{uuid.uuid4().hex}.pdf"
    )


class GrantDocument(models.Model):
    class DocumentType(models.TextChoices):
        CONTRACT_AMENDMENT = (
            "CONTRACT_AMENDMENT",
            "Contract/Amendment",
        )
        CORRESPONDENCE = (
            "CORRESPONDENCE",
            "Correspondence",
        )

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REPLACED = "REPLACED", "Replaced"
        DELETED = "DELETED", "Deleted"

    grant_id = models.CharField(
        max_length=10,
        db_index=True,
        validators=[GRANT_ID_VALIDATOR],
    )

    change_request = models.ForeignKey(
        ChangeRequest,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="documents",
    )

    document_type = models.CharField(
        max_length=25,
        choices=DocumentType.choices,
    )

    amendment_no = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[
            MinValueValidator(0),
            MaxValueValidator(99),
        ],
    )

    correspondence_date = models.DateField(
        null=True,
        blank=True,
    )

    correspondence_sequence = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[
            MinValueValidator(1),
            MaxValueValidator(99),
        ],
    )

    file = models.FileField(
        upload_to=grant_document_upload_path,
        validators=[
            FileExtensionValidator(
                allowed_extensions=["pdf"],
            )
        ],
        max_length=500,
        blank=True,
    )

    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField()

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_grant_documents",
    )

    uploaded_at = models.DateTimeField(auto_now_add=True)

    replaces_document = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replacement_document",
    )

    replaced_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replaced_grant_documents",
    )

    replaced_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deleted_grant_documents",
    )

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    purged_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "grant_document"
        ordering = ["grant_id", "document_type", "uploaded_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                        models.Q(
                            document_type="CONTRACT_AMENDMENT",
                            amendment_no__isnull=False,
                            amendment_no__gte=0,
                            amendment_no__lte=99,
                            correspondence_date__isnull=True,
                            correspondence_sequence__isnull=True,
                        )
                        |
                        models.Q(
                            document_type="CORRESPONDENCE",
                            amendment_no__isnull=True,
                            correspondence_date__isnull=False,
                            correspondence_sequence__isnull=False,
                            correspondence_sequence__gte=1,
                            correspondence_sequence__lte=99,
                        )
                ),
                name="valid_grant_document_metadata",
            ),
            models.CheckConstraint(
                condition=(
                        models.Q(
                            status="ACTIVE",
                            replaced_by__isnull=True,
                            replaced_at__isnull=True,
                            deleted_by__isnull=True,
                            deleted_at__isnull=True,
                            purged_at__isnull=True,
                        )
                        |
                        models.Q(
                            status="REPLACED",
                            replaced_by__isnull=False,
                            replaced_at__isnull=False,
                            deleted_by__isnull=True,
                            deleted_at__isnull=True,
                        )
                        |
                        models.Q(
                            status="DELETED",
                            replaced_by__isnull=True,
                            replaced_at__isnull=True,
                            deleted_by__isnull=False,
                            deleted_at__isnull=False,
                        )
                ),
                name="valid_grant_document_status_metadata",
            ),
            models.UniqueConstraint(
                fields=[
                    "grant_id",
                    "amendment_no",
                ],
                condition=models.Q(
                    status="ACTIVE",
                    document_type="CONTRACT_AMENDMENT",
                ),
                name="unique_active_contract_amendment",
            ),
            models.UniqueConstraint(
                fields=[
                    "grant_id",
                    "correspondence_date",
                    "correspondence_sequence",
                ],
                condition=models.Q(
                    status="ACTIVE",
                    document_type="CORRESPONDENCE",
                ),
                name="unique_active_correspondence",
            ),
        ]

    @property
    def display_filename(self):
        if self.document_type == self.DocumentType.CONTRACT_AMENDMENT:
            return (
                f"{self.grant_id}-A-"
                f"{self.amendment_no:02d}.pdf"
            )

        return (
            f"{self.grant_id}-B-"
            f"{self.correspondence_date:%Y-%m-%d}-"
            f"{self.correspondence_sequence:02d}.pdf"
        )

    def __str__(self):
        return self.display_filename


class GrantDocumentAction(models.Model):
    class Action(models.TextChoices):
        UPLOAD = "UPLOAD", "Upload"
        EDIT_METADATA = "EDIT_METADATA", "Edit Metadata"
        REPLACE_FILE = "REPLACE_FILE", "Replace File"
        DELETE = "DELETE", "Delete"
        PURGE = "PURGE", "Purge Physical File"

    document = models.ForeignKey(
        GrantDocument,
        on_delete=models.PROTECT,
        related_name="action_history",
    )

    action = models.CharField(
        max_length=20,
        choices=Action.choices,
    )

    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="grant_document_actions",
    )

    acted_at = models.DateTimeField(auto_now_add=True)

    reason = models.TextField(
        blank=True,
        default="",
        max_length=500,
    )

    details = models.TextField(
        blank=True,
        default="",
        max_length=1000,
    )

    class Meta:
        db_table = "grant_document_action"
        ordering = ["acted_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                        models.Q(action="PURGE")
                        |
                        models.Q(acted_by__isnull=False)
                ),
                name="document_action_actor_required",
            ),
        ]

    @property
    def actor_display(self):
        if self.acted_by:
            return (
                self.acted_by.get_full_name()
                or self.acted_by.username
            )

        if self.action == self.Action.PURGE:
            return "System Purge"

        return "System"

    def __str__(self):
        return (
            f"{self.get_action_display()} for "
            f"{self.document.display_filename} "
            f"by {self.actor_display}"
        )
