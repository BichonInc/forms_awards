from django import forms
from .models import Form1
from django.core.exceptions import ValidationError
import re
class GrantForm(forms.ModelForm):
    # Define extra fields for 'Add New' options
    new_program_title = forms.CharField(required=False, label='New Program Title')
    new_contracting_agency = forms.CharField(required=False, label='New Contracting Agency')
    new_federal_grantor = forms.CharField(required=False, label='New Federal Grantor')
    new_federal_aln = forms.CharField(required=False, label='New Federal ALN')

    class Meta:
        model = Form1
        fields = [
            'grant_id', 'program_title', 'contracting_agency', 'contract_number',
            'contract_start_date', 'contract_end_date', 'contract_amount',
            'federal_grantor', 'federal_aln', 'internal_award_code',
            'internal_gl_start_date', 'internal_gl_end_date', 'status'
        ]
        widgets = {
            'grant_id': forms.HiddenInput(),  # Keep grant_id hidden and non-editable
            'contract_start_date': forms.DateInput(attrs={'type': 'date'}),  # Use HTML5 date input
            'contract_end_date': forms.DateInput(attrs={'type': 'date'}),
            'internal_gl_start_date': forms.DateInput(attrs={'type': 'date'}),
            'internal_gl_end_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def clean_federal_aln(self):
        federal_aln = self.cleaned_data.get('federal_aln')
        # Skip validation if 'Add New' is selected
        if federal_aln == 'Add New':
            return federal_aln
        # Allow "None" as a valid value
        if federal_aln == "None":
            return None
        # Validate federal_aln format
        if federal_aln and not re.match(r'^\d{2}\.\d{3}$', federal_aln):
            raise forms.ValidationError(
                "Federal ALN must be in the format 'xx.xxx', where x is a digit."
            )
        return federal_aln

    def clean_internal_award_code(self):
        internal_award_code = self.cleaned_data.get('internal_award_code')

        # Ensure it's an integer before comparison
        try:
            internal_award_code_int = int(internal_award_code)
        except ValueError:
            raise forms.ValidationError("Internal Award Code must be a number between 100 and 999.")

        # Validate range
        if internal_award_code_int < 100 or internal_award_code_int > 999:
            raise forms.ValidationError("Internal Award Code must be between 100 and 999.")

        return internal_award_code

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Get current choices from the database and handle None values
        program_titles = sorted(pt for pt in Form1.objects.values_list('program_title', flat=True).distinct() if pt)
        contracting_agencies = sorted(
            ca for ca in Form1.objects.values_list('contracting_agency', flat=True).distinct() if ca)
        federal_grantors = sorted(fg for fg in Form1.objects.values_list('federal_grantor', flat=True).distinct() if fg)
        federal_alns = sorted(fa for fa in Form1.objects.values_list('federal_aln', flat=True).distinct() if fa)

        # Set choices dynamically with "Select" first, "Add New" second, and sorted values afterward
        self.fields['program_title'].widget = forms.Select(
            choices=[('', 'Select'), ('Add New', 'Add New')] + [(pt, pt) for pt in program_titles]
        )
        self.fields['contracting_agency'].widget = forms.Select(
            choices=[('', 'Select'), ('Add New', 'Add New')] + [(ca, ca) for ca in contracting_agencies]
        )
        self.fields['federal_grantor'].widget = forms.Select(
            choices=[('', 'Select'), ('Add New', 'Add New')] + [(fg, fg) for fg in federal_grantors]
        )
        self.fields['federal_aln'].widget = forms.Select(
            choices=[('', 'Select'), ('Add New', 'Add New')] + [(fa, fa) for fa in federal_alns]
        )

    def clean(self):
        print("Clean method executed")  # Debugging

        cleaned_data = super().clean()

        # === Date Validation ===
        contract_start_date = cleaned_data.get('contract_start_date')
        contract_end_date = cleaned_data.get('contract_end_date')
        internal_gl_start_date = cleaned_data.get('internal_gl_start_date')
        internal_gl_end_date = cleaned_data.get('internal_gl_end_date')

        # Debugging prints for dates
        print(f"Contract Start Date: {contract_start_date}")
        print(f"Contract End Date: {contract_end_date}")
        print(f"Internal GL Start Date: {internal_gl_start_date}")
        print(f"Internal GL End Date: {internal_gl_end_date}")

        # Contract dates validation
        if contract_start_date and contract_end_date and contract_end_date < contract_start_date:
            print("Validation failed: Contract end date is earlier than start date")
            self.add_error('contract_end_date', "Contract end date must be on or after contract start date.")

        # Internal GL dates validation
        if internal_gl_start_date and internal_gl_end_date and internal_gl_end_date < internal_gl_start_date:
            print("Validation failed: Internal GL end date is earlier than start date")
            self.add_error('internal_gl_end_date', "Internal GL end date must be on or after internal GL start date.")

        # === "Add New" Handling ===
        federal_aln = cleaned_data.get('federal_aln')  # Get the current value of federal_aln
        new_federal_aln = cleaned_data.get('new_federal_aln')  # Get the value entered in the "Add New" textbox

        if federal_aln == 'Add New':
            # Validate the "new_federal_aln" field
            if not new_federal_aln:
                self.add_error('new_federal_aln', "You must enter a new Federal ALN when selecting 'Add New'.")
            elif not re.match(r'^\d{2}\.\d{3}$', new_federal_aln):
                self.add_error('new_federal_aln', "Federal ALN must be in the format 'xx.xxx', where x is a digit.")
            else:
                # Assign the new Federal ALN to the 'federal_aln' field in cleaned_data
                cleaned_data['federal_aln'] = new_federal_aln
        elif federal_aln:
            # Validate existing 'federal_aln' values (not "Add New")
            if not re.match(r'^\d{2}\.\d{3}$', federal_aln):
                self.add_error('federal_aln', "Federal ALN must be in the format 'xx.xxx', where x is a digit.")

        # Handle "Add New" for other fields (Program Title, Contracting Agency, Federal Grantor)
        if cleaned_data.get('program_title') == 'Add New':
            new_program_title = cleaned_data.get('new_program_title')
            if not new_program_title:
                self.add_error('new_program_title', "You must enter a new Program Title when selecting 'Add New'.")
            cleaned_data['program_title'] = new_program_title

        if cleaned_data.get('contracting_agency') == 'Add New':
            new_contracting_agency = cleaned_data.get('new_contracting_agency')
            if not new_contracting_agency:
                self.add_error('new_contracting_agency',
                               "You must enter a new Contracting Agency when selecting 'Add New'.")
            cleaned_data['contracting_agency'] = new_contracting_agency

        if cleaned_data.get('federal_grantor') == 'Add New':
            new_federal_grantor = cleaned_data.get('new_federal_grantor')
            if not new_federal_grantor:
                self.add_error('new_federal_grantor', "You must enter a new Federal Grantor when selecting 'Add New'.")
            cleaned_data['federal_grantor'] = new_federal_grantor

        # Validate Internal Award Code
        internal_award_code = cleaned_data.get('internal_award_code')
        if internal_award_code and not re.match(r'^\d{3}$', internal_award_code):
            self.add_error('internal_award_code', "Internal Award Code must be between 100 and 999.")

        return cleaned_data





