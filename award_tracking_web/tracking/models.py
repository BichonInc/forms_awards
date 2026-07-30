from django.conf import settings
from django.db import models
from decimal import Decimal


from django.core.validators import RegexValidator, MinValueValidator, MaxValueValidator


class Form1(models.Model):
    grant_id = models.CharField(max_length=10, primary_key=True)
    program_title = models.CharField(max_length=255)
    contracting_agency = models.CharField(max_length=255)
    contract_number = models.CharField(max_length=50, null=False)
    contract_start_date = models.DateTimeField(null=True, blank=True)
    contract_end_date = models.DateTimeField(null=True, blank=True)
    contract_amount = models.DecimalField(max_digits=10, decimal_places=2)
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
    grant_id = models.ForeignKey(Form1, on_delete=models.CASCADE, db_column='grant_id')
    fiscal_year = models.CharField(max_length=7)  # FY22-23 format
    federal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    nonfederal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
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
    grant_id = models.ForeignKey(Form1, on_delete=models.CASCADE, db_column='grant_id')
    fiscal_year = models.CharField(max_length=10)
    federal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    nonfederal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))


class ChangeRequest(models.Model):
    class RequestType(models.TextChoices):
        NEW_GRANT = "NEW_GRANT", "New Grant"
        EDIT_GRANT = "EDIT_GRANT", "Edit Grant"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RETURNED = "RETURNED", "Returned for Revision"
        DENIED = "DENIED", "Denied"
        APPROVED = "APPROVED", "Approved"

    grant_id = models.CharField(max_length=10)
    request_type = models.CharField(
        max_length=20,
        choices=RequestType.choices,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
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
