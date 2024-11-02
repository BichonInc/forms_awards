
from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Sum, Max
from .models import GLExpenditure, Form1, FiscalBreakdown
from .forms import GrantForm
from django.core.files.storage import default_storage
import pandas as pd
import sqlite3
from datetime import datetime, date
from decimal import Decimal, InvalidOperation # Import this to ensure consistent types
from django.conf import settings
import os
import logging

# Function to generate new grant_id
def generate_new_grant_id():
    last_grant = Form1.objects.aggregate(last_id=Max('grant_id'))
    last_id = last_grant['last_id']
    if last_id:
        numeric_part = int(last_id[1:]) + 1
        new_grant_id = f'A{numeric_part:05d}'
    else:
        new_grant_id = 'A00001'
    return new_grant_id

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



def grant_list(request):
    grants = Form1.objects.all()

    # Loop through each grant and fetch fiscal data to check for non-zero differences
    for grant in grants:
        fiscal_data = get_fiscal_data(grant.grant_id)  # Fetch fiscal data for the grant
        grant.has_difference = any(row['difference'] != 0 for row in fiscal_data)  # Add a flag for difference

    return render(request, 'tracking/grant_list.html', {'grants': grants})


def grant_detail(request, grant_id):
    print(f"Grant detail accessed for grant_id: {grant_id}")

    # Fetch the grant object from form_1 using grant_id
    grant = get_object_or_404(Form1, grant_id=grant_id)
    print(f"Grant object fetched: {grant}")

    # Process form submission
    if request.method == 'POST':
        print("Form submission detected")

        # Handle the main grant form (if you need to update grant fields)
        form = GrantForm(request.POST, instance=grant)
        if form.is_valid():
            form.save()
            print("Grant form saved.")

        # Handle user inputs for federal and non-federal in fiscal breakdown
        gl_expenditures = GLExpenditure.objects.filter(grant_id=grant_id)
        fiscal_breakdown = gl_expenditures.values('fiscal_year').annotate(
            total_expenditure=Sum('net_expenditure')
        ).order_by('fiscal_year')

        # Loop through fiscal breakdown and save/update based on user input
        for i, breakdown in enumerate(fiscal_breakdown, start=1):
            fiscal_year = breakdown['fiscal_year']
            try:
                federal_input = Decimal(request.POST.get(f'federal_{i}', '0').replace(',', ''))
            except InvalidOperation:
                federal_input = Decimal('0')
                print(f"Invalid input for federal_{i}; defaulting to 0")

            try:
                nonfederal_input = Decimal(request.POST.get(f'nonfederal_{i}', '0').replace(',', ''))
            except InvalidOperation:
                nonfederal_input = Decimal('0')
                print(f"Invalid input for nonfederal_{i}; defaulting to 0")

            # Fetch or create fiscal breakdown record
            fiscal_record, created = FiscalBreakdown.objects.get_or_create(
                grant_id=grant,
                fiscal_year=fiscal_year,
                defaults={'federal': federal_input, 'nonfederal': nonfederal_input}
            )

            # Update if record exists
            if not created:
                fiscal_record.federal = federal_input
                fiscal_record.nonfederal = nonfederal_input
                fiscal_record.save()

            print(
                f"Fiscal record for {fiscal_year} - Federal: {fiscal_record.federal}, Nonfederal: {fiscal_record.nonfederal}")

        return redirect('grant_detail', grant_id=grant_id)

    # Displaying the grant detail page (GET request)
    form = GrantForm(instance=grant)
    gl_expenditures = GLExpenditure.objects.filter(grant_id=grant_id)

    # Aggregate total expenditure
    total_expenditure_value = gl_expenditures.aggregate(
        total_sum=Sum('net_expenditure')
    ).get('total_sum') or Decimal('0')

    # Get the latest expenditure date
    last_expenditure_date = (
        gl_expenditures.latest('effective_date').effective_date
        if gl_expenditures.exists() else None
    )

    # Fiscal year breakdown
    fiscal_breakdown = gl_expenditures.values('fiscal_year').annotate(
        total_expenditure=Sum('net_expenditure')
    ).order_by('fiscal_year')

    # Calculate totals and differences
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

    return render(request, 'tracking/grant_detail.html', {
        'grant': grant,
        'form': form,
        'total_expenditure_value': total_expenditure_value,
        'last_expenditure_date': last_expenditure_date,
        'fiscal_breakdown': fiscal_breakdown,
        'total_expenditure_sum': total_expenditure_sum,
        'total_federal_sum': total_federal_sum,
        'total_nonfederal_sum': total_nonfederal_sum,
        'total_difference': total_difference
    })





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
            # Save the form
            form.save()

            # Handle 'Add New' for dropdowns
            if form.cleaned_data['program_title'] == 'Add New':
                form.instance.program_title = form.cleaned_data['new_program_title']
            if form.cleaned_data['contracting_agency'] == 'Add New':
                form.instance.contracting_agency = form.cleaned_data['new_contracting_agency']
            if form.cleaned_data['federal_grantor'] == 'Add New':
                form.instance.federal_grantor = form.cleaned_data['new_federal_grantor']
            if form.cleaned_data['federal_aln'] == 'Add New':
                form.instance.federal_aln = form.cleaned_data['new_federal_aln']

            # Save the form and redirect
            form.save()
            return redirect('grant_list')
        else:
            # Debug: Print form errors
            print(f"Form is not valid. Errors: {form.errors}")

    else:
        form = GrantForm()

    return render(request, 'tracking/grant_form.html', {'form': form})


def grant_edit(request, grant_id):
    grant = get_object_or_404(Form1, grant_id=grant_id)
    if request.method == 'POST':
        form = GrantForm(request.POST, instance=grant)
        if form.is_valid():
            # Check if the form has new values and update accordingly
            new_program_title = form.cleaned_data.get('new_program_title')
            new_contracting_agency = form.cleaned_data.get('new_contracting_agency')
            new_federal_grantor = form.cleaned_data.get('new_federal_grantor')
            new_federal_aln = form.cleaned_data.get('new_federal_aln')

            if new_program_title:
                form.instance.program_title = new_program_title
            if new_contracting_agency:
                form.instance.contracting_agency = new_contracting_agency
            if new_federal_grantor:
                form.instance.federal_grantor = new_federal_grantor
            if new_federal_aln:
                form.instance.federal_aln = new_federal_aln

            form.save()
            return redirect('grant_detail', grant_id=grant.grant_id)
    else:
        form = GrantForm(instance=grant)
    return render(request, 'tracking/grant_form.html', {'form': form, 'edit': True})


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
