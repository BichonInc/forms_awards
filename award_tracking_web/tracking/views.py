
from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Sum, Max
from .models import Form1, GLExpenditure
from .forms import GrantForm

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

def grant_list(request):
    grants = Form1.objects.all()
    return render(request, 'tracking/grant_list.html', {'grants': grants})

def grant_detail(request, grant_id):
    # Fetch the grant object
    grant = get_object_or_404(Form1, grant_id=grant_id)

    if request.method == 'POST':
        # Handle form submission for updating grant details
        form = GrantForm(request.POST, instance=grant)
        if form.is_valid():
            form.save()
            return redirect('grant_detail', grant_id=grant_id)
    else:
        form = GrantForm(instance=grant)

    # Calculate expenditure details
    gl_expenditures = GLExpenditure.objects.filter(
        award_code=grant.internal_award_code,
        effective_date__gte=grant.internal_gl_start_date,
        effective_date__lte=grant.internal_gl_end_date
    )

    total_expenditure = gl_expenditures.aggregate(
        federal_sum=Sum('expenditure_federal'),
        nonfederal_sum=Sum('expenditure_nonfederal')
    )

    # Safely handle None values by using 0 as the default if None
    federal_sum = total_expenditure.get('federal_sum') or 0
    nonfederal_sum = total_expenditure.get('nonfederal_sum') or 0
    total_expenditure_value = federal_sum + nonfederal_sum

    last_expenditure_date = (
        gl_expenditures.latest('effective_date').effective_date
        if gl_expenditures.exists() else None
    )

    # Fiscal year breakdown
    fiscal_breakdown = gl_expenditures.values('fiscal_year').annotate(
        expenditure_federal_sum=Sum('expenditure_federal'),
        expenditure_nonfederal_sum=Sum('expenditure_nonfederal')
    )

    return render(request, 'tracking/grant_detail.html', {
        'grant': grant,
        'form': form,
        'total_expenditure_value': total_expenditure_value,
        'last_expenditure_date': last_expenditure_date,
        'fiscal_breakdown': fiscal_breakdown
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