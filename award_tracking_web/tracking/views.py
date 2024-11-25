
from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Sum, Max
from .models import Form1, GLExpenditure, FiscalBreakdown, SubsequentAdjustment, SubsequentFiscalBreakdown
from .forms import GrantForm
from django.core.files.storage import default_storage
import pandas as pd
import sqlite3
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

def get_fiscal_data(grant_id):
    db_path = settings.DATABASES['default']['NAME']
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Adjusted query to group data by grant_id and fiscal_year, summing the net_expenditure
    query = '''
        SELECT g.fiscal_year, SUM(g.net_expenditure) AS total_expenditure, f.federal, f.nonfederal 
        FROM gl_expenditure g
        LEFT JOIN fiscal_breakdown f ON g.grant_id = f.grant_id AND g.fiscal_year = f.fiscal_year
        WHERE g.grant_id = ?
        GROUP BY g.fiscal_year
    '''
    cursor.execute(query, (grant_id,))
    fiscal_data = cursor.fetchall()
    print(fiscal_data)
    conn.close()

    fiscal_data_with_difference = []
    for row in fiscal_data:
        fiscal_year, total_expenditure, federal, nonfederal = row
        difference = total_expenditure - (federal or 0) - (nonfederal or 0)
        difference = round(difference, 2)
        fiscal_data_with_difference.append({
            'fiscal_year': fiscal_year,
            'total_expenditure': total_expenditure,
            'federal': federal,
            'nonfederal': nonfederal,
            'difference': difference,
        })
    print(fiscal_data_with_difference)
    return fiscal_data_with_difference
def get_subsequent_data(grant_id):
    db_path = settings.DATABASES['default']['NAME']
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Query to group data by grant_id and fiscal_year, summing net_expenditure for Subsequent Adjustment
    query = '''
        SELECT sa.fiscal_year, SUM(sa.net_expenditure) AS total_adjustment, sf.federal, sf.nonfederal 
        FROM tracking_subsequentadjustment sa
        LEFT JOIN tracking_subsequentfiscalbreakdown sf ON sa.grant_id = sf.grant_id AND sa.fiscal_year = sf.fiscal_year
        WHERE sa.grant_id = ?
        GROUP BY sa.fiscal_year
    '''
    cursor.execute(query, (grant_id,))
    subsequent_data = cursor.fetchall()
    print(subsequent_data)
    conn.close()

    # Calculate the difference and round it to 2 decimal points
    subsequent_data_with_difference = []
    for row in subsequent_data:
        fiscal_year, total_adjustment, federal, nonfederal = row
        difference = total_adjustment - (federal or 0) - (nonfederal or 0)
        difference = round(difference, 2)
        subsequent_data_with_difference.append({
            'fiscal_year': fiscal_year,
            'total_adjustment': total_adjustment,
            'federal': federal,
            'nonfederal': nonfederal,
            'difference': difference,
        })
    print(subsequent_data_with_difference)
    return subsequent_data_with_difference



# In views.py
def grant_list(request):
    grants = Form1.objects.all().order_by('grant_id')

    for grant in grants:
        grant_id = grant.grant_id

        # Check In-Period Warning
        in_period_warning = any(
            row['difference'] != 0 for row in get_fiscal_data(grant_id)
        )

        # Check Subsequent Adjustment Warning
        subsequent_warning = any(
            row['difference'] != 0 for row in get_subsequent_data(grant_id)
        )

        # Add attributes to the grant object
        grant.has_in_period_difference = in_period_warning
        grant.has_subsequent_difference = subsequent_warning

    context = {
        'grants': grants,
    }
    return render(request, 'tracking/grant_list.html', context)




def grant_detail(request, grant_id):
    print(f"Grant detail accessed for grant_id: {grant_id}")

    # Fetch the grant object from form_1 using grant_id
    grant = get_object_or_404(Form1, grant_id=grant_id)
    print(f"Grant object fetched: {grant}")

    if request.method == 'POST':
        form_type = request.POST.get('form_type')
        if form_type == 'basic':
            print("Basic form submission detected")
            form = GrantForm(request.POST, instance=grant)
            if form.is_valid():
                form.save()
                print("Grant Basic Information saved.")
        elif form_type == 'fiscal':
            print("Fiscal form submission detected")
            # Handle user inputs for In-Period Expenditure section
            gl_expenditures = GLExpenditure.objects.filter(grant_id=grant_id)
            fiscal_breakdown = gl_expenditures.values('fiscal_year').annotate(
                total_expenditure=Sum('net_expenditure')
            ).order_by('fiscal_year')

            for i, breakdown in enumerate(fiscal_breakdown, start=1):
                fiscal_year = breakdown['fiscal_year']
                try:
                    federal_input = Decimal(request.POST.get(f'federal_{i}', '0').replace(',', ''))
                except InvalidOperation:
                    federal_input = Decimal('0')

                try:
                    nonfederal_input = Decimal(request.POST.get(f'nonfederal_{i}', '0').replace(',', ''))
                except InvalidOperation:
                    nonfederal_input = Decimal('0')

                fiscal_record, created = FiscalBreakdown.objects.get_or_create(
                    grant_id=grant,
                    fiscal_year=fiscal_year,
                    defaults={'federal': federal_input, 'nonfederal': nonfederal_input}
                )

                if not created:
                    fiscal_record.federal = federal_input
                    fiscal_record.nonfederal = nonfederal_input
                    fiscal_record.save()
        elif form_type == 'subsequent':
            print("Subsequent Adjustment form submission detected")
            # Handle user inputs for Subsequent Adjustment section
            subsequent_adjustments = SubsequentAdjustment.objects.filter(grant_id=grant_id)
            subsequent_breakdown = subsequent_adjustments.values('fiscal_year').annotate(
                total_expenditure=Sum('net_expenditure')
            ).order_by('fiscal_year')

            for i, breakdown in enumerate(subsequent_breakdown, start=1):
                fiscal_year = breakdown['fiscal_year']
                try:
                    federal_input = Decimal(request.POST.get(f'federal_subsequent_{i}', '0').replace(',', ''))
                except InvalidOperation:
                    federal_input = Decimal('0')

                try:
                    nonfederal_input = Decimal(request.POST.get(f'nonfederal_subsequent_{i}', '0').replace(',', ''))
                except InvalidOperation:
                    nonfederal_input = Decimal('0')

                subsequent_record, created = SubsequentFiscalBreakdown.objects.get_or_create(
                    grant_id=grant,
                    fiscal_year=fiscal_year,
                    defaults={'federal': federal_input, 'nonfederal': nonfederal_input}
                )

                if not created:
                    subsequent_record.federal = federal_input
                    subsequent_record.nonfederal = nonfederal_input
                    subsequent_record.save()

            return redirect('grant_detail', grant_id=grant_id)

    # Displaying the grant detail page (GET request)
    form = GrantForm(instance=grant)

    # Fetch fiscal year breakdown for GL Expenditure
    gl_expenditures = GLExpenditure.objects.filter(grant_id=grant_id)
    fiscal_breakdown = gl_expenditures.values('fiscal_year').annotate(
        total_expenditure=Sum('net_expenditure')
    ).order_by('fiscal_year')

    total_expenditure_sum = Decimal('0')
    total_federal_sum = Decimal('0')
    total_nonfederal_sum = Decimal('0')
    total_difference = Decimal('0')

    for breakdown in fiscal_breakdown:
        breakdown_record = FiscalBreakdown.objects.filter(
            grant_id=grant, fiscal_year=breakdown['fiscal_year']
        ).first()

        federal = breakdown_record.federal if breakdown_record else Decimal('0')
        nonfederal = breakdown_record.nonfederal if breakdown_record else Decimal('0')

        breakdown['federal'] = federal
        breakdown['nonfederal'] = nonfederal
        breakdown['difference'] = breakdown['total_expenditure'] - federal - nonfederal

        total_expenditure_sum += breakdown['total_expenditure']
        total_federal_sum += federal
        total_nonfederal_sum += nonfederal
        total_difference += breakdown['difference']

    # Fetch fiscal year breakdown for Subsequent Adjustment
    subsequent_adjustments = SubsequentAdjustment.objects.filter(grant_id=grant_id)
    subsequent_breakdown = subsequent_adjustments.values('fiscal_year').annotate(
        total_expenditure=Sum('net_expenditure')
    ).order_by('fiscal_year')

    # Calculate totals for Subsequent Adjustment
    total_adjustment_sum = Decimal('0')
    total_federal_sub_sum = Decimal('0')
    total_nonfederal_sub_sum = Decimal('0')
    total_difference_sub = Decimal('0')

    print("Subsequent Adjustment Data:")
    for breakdown in subsequent_breakdown:
        sub_record = SubsequentFiscalBreakdown.objects.filter(
            grant_id=grant, fiscal_year=breakdown['fiscal_year']
        ).first()
        print(breakdown)
        federal = sub_record.federal if sub_record else Decimal('0')
        nonfederal = sub_record.nonfederal if sub_record else Decimal('0')

        breakdown['federal'] = federal
        breakdown['nonfederal'] = nonfederal
        breakdown['difference'] = breakdown['total_expenditure'] - federal - nonfederal

        total_adjustment_sum += breakdown['total_expenditure']
        total_federal_sub_sum += breakdown['federal']
        total_nonfederal_sub_sum += breakdown['nonfederal']
        total_difference_sub += breakdown['difference']

    print("Context for subsequent_breakdown:", subsequent_breakdown)

    return render(request, 'tracking/grant_detail.html', {
        'grant': grant,
        'form': form,
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
    })

from django.db.models import Q
from django.contrib import messages

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


def grant_delete(request, grant_id):
    grant = get_object_or_404(Form1, grant_id=grant_id)
    if request.method == 'POST':
        grant.delete()
        return redirect('grant_list')
    return render(request, 'tracking/grant_delete.html', {'grant': grant})



logger = logging.getLogger(__name__)

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

            # Update grant_id associations based on internal award codes
            for expenditure in GLExpenditure.objects.all():
                try:
                    matching_form = Form1.objects.get(
                        internal_award_code=expenditure.award_code,
                        internal_gl_start_date__lte=expenditure.effective_date,
                        internal_gl_end_date__gte=expenditure.effective_date
                    )
                    expenditure.grant_id = matching_form.grant_id
                    expenditure.save()
                except Form1.DoesNotExist:
                    expenditure.grant_id = None
                    expenditure.save()

            logger.info("GL Expenditure data successfully refreshed.")
            return redirect('grant_list')  # Redirect to refresh Grant List page

        except Exception as e:
            logger.error(f"An error occurred while processing the file: {str(e)}")
            return render(request, 'tracking/grant_list.html', {
                'message': f'An error occurred while processing the file: {str(e)}'
            })

    # If not a POST request or no file provided
    return render(request, 'tracking/grant_list.html', {
        'message': 'Please upload a valid Excel file.'
    })


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

        # Write GL Expenditure grouped data
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
                form.contract_start_date, form.contract_end_date, form.contract_amount,
                form.federal_grantor, form.federal_aln, form.internal_award_code,
                form.internal_gl_start_date, form.internal_gl_end_date, form.status,
                'GL Expenditure', fiscal_year, total_expenditure, federal,
                nonfederal, difference
            ])

        # Grouped data from SubsequentAdjustment by fiscal_year
        subsequent_adjustments = SubsequentAdjustment.objects.filter(grant_id=grant_id).values('fiscal_year').annotate(
            total_adjustment=Sum('net_expenditure')
        ).order_by('fiscal_year')

        # Write Subsequent Adjustment grouped data
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
                form.contract_start_date, form.contract_end_date, form.contract_amount,
                form.federal_grantor, form.federal_aln, form.internal_award_code,
                form.internal_gl_start_date, form.internal_gl_end_date, form.status,
                'Subsequent Adjustment', fiscal_year, total_adjustment, federal_sub,
                nonfederal_sub, difference_sub
            ])

    return response
