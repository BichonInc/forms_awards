from django import forms
from .models import Form1

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
        cleaned_data = super().clean()

        # Handle 'Add New' logic for each field
        if cleaned_data.get('program_title') == 'Add New':
            cleaned_data['program_title'] = cleaned_data.get('new_program_title')
        if cleaned_data.get('contracting_agency') == 'Add New':
            cleaned_data['contracting_agency'] = cleaned_data.get('new_contracting_agency')
        if cleaned_data.get('federal_grantor') == 'Add New':
            cleaned_data['federal_grantor'] = cleaned_data.get('new_federal_grantor')
        if cleaned_data.get('federal_aln') == 'Add New':
            cleaned_data['federal_aln'] = cleaned_data.get('new_federal_aln')

        return cleaned_data
