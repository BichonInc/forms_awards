from django.db import models
from django.db import models


class Form1(models.Model):
    grant_id = models.CharField(max_length=10, primary_key=True)
    program_title = models.CharField(max_length=255)
    contracting_agency = models.CharField(max_length=255)
    contract_number = models.CharField(max_length=50, null= False)
    contract_start_date = models.DateTimeField(null=True, blank=True)
    contract_end_date = models.DateTimeField(null=True, blank=True)
    contract_amount = models.DecimalField(max_digits=10, decimal_places=2)
    federal_grantor = models.CharField(max_length=255, null=True, blank=True)
    federal_aln = models.CharField(max_length=255, null=True, blank=True)
    internal_award_code = models.CharField(max_length=10)
    internal_gl_start_date = models.DateTimeField(null=True, blank=True)
    internal_gl_end_date = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=[('active', 'Active'), ('inactive', 'Inactive')])
    class Meta:
        db_table = 'form_1'  # Use the existing table name


class GLExpenditure(models.Model):
    effective_date = models.DateField()
    award_code = models.CharField(max_length=10)
    debit = models.DecimalField(max_digits=10, decimal_places=2)
    credit = models.DecimalField(max_digits=10, decimal_places=2)
    net_expenditure = models.DecimalField(max_digits=10, decimal_places=2)
    fiscal_year = models.CharField(max_length=7)
    grant_id = models.CharField(max_length=10)
    expenditure_federal = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    expenditure_nonfederal = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = 'gl_expenditure'  # Use the existing table name
