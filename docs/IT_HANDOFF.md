# GrantWise – IT Handoff and Production Upgrade Guide

**Application:** GrantWise
**Framework:** Django 5.1.1 / Python 3.12
**Database:** SQLite
**Repository:** `BichonInc/forms_awards`
**Current development branch:** `new-grant-approval-workflow`
**Current release checkpoint:** `b71ea97 – Consolidate Python deployment requirements`
**Phase One baseline:** `master` at commit `9c9334b – Add missing migrations and repository gitignore`

---

## 1. Purpose of This Document

This document provides the technical handoff information needed to review, deploy, and support the current GrantWise release.

The production environment already contains the Phase One version of GrantWise. Therefore, this release must be treated as an **upgrade of the existing application and production database**, not as a new installation.

The production database must be preserved and upgraded through Django migrations. The development SQLite database must not be copied over or substituted for the production database.

---

## 2. Application Purpose

GrantWise is a grant and contract tracking application designed to support organizations managing grants from multiple funding sources, including federal, state, county, city, healthcare, and private organizations.

Its primary functions include:

- maintaining grant and contract basic information;
- associating General Ledger expenditures with the correct grant period;
- supporting reused internal award codes across multiple contracts;
- tracking Federal and Nonfederal expenditure allocations;
- tracking Subsequent Adjustments;
- identifying fiscal records requiring review;
- maintaining an approval workflow for changes to grant information;
- preserving change-request history and audit information;
- supporting Schedule of Expenditures of Federal Awards (SEFA) preparation and reporting.

The application uses grant contract dates and Internal GL dates to associate accounting activity with the appropriate grant record.

---

# 3. Current Technical Environment

The development environment currently uses:

- Windows
- Python 3.12
- Django 5.1.1
- SQLite
- pandas 2.2.3
- openpyxl 3.1.5
- Git / GitHub

The Django project is located under:

```text
award_tracking_web
```

The primary Django application is:

```text
tracking
```

---

# 4. Python Dependencies

There is now one canonical dependency file:

```text
/requirements.txt
```

The previous duplicate:

```text
/award_tracking_web/requirements.txt
```

has been removed.

The root `requirements.txt` is UTF-8 encoded and contains:

```text
asgiref==3.8.1
Django==5.1.1
et-xmlfile==1.1.0
numpy==2.1.2
openpyxl==3.1.5
pandas==2.2.3
python-dateutil==2.9.0.post0
pytz==2024.2
six==1.16.0
sqlparse==0.5.1
tzdata==2024.1
```

Deployment should therefore use:

```powershell
python -m pip install -r requirements.txt
```

The dependency list was verified against the working development virtual environment using:

```powershell
python -m pip install --dry-run --no-index -r requirements.txt
python -m pip check
```

All required versions were already installed and `pip check` reported:

```text
No broken requirements found.
```

---

# 5. Phase One Baseline

Phase One was deployed from the `master` branch.

The verified Phase One Git baseline is:

```text
9c9334b – Add missing migrations and repository gitignore
```

The `master` branch contains exactly these `tracking` migrations:

```text
0001_initial.py
0002_fiscalbreakdown.py
0003_alter_fiscalbreakdown_table.py
0004_alter_fiscalbreakdown_grant_id.py
0005_subsequentadjustment_subsequentfiscalbreakdown.py
0006_alter_subsequentadjustment_grant_id_and_more.py
```

No migrations later than `0006` exist in the Phase One source baseline.

The current release adds migrations:

```text
0007 through 0025
```

IT must still verify the actual production database migration state before deployment. The source-code baseline alone does not prove that every migration was successfully applied in production.

Run:

```powershell
python award_tracking_web/manage.py showmigrations tracking
```

If production is current through `tracking.0006`, Django should apply `0007` through `0025` in sequence.

---

# 6. Core Grant Information

The primary grant table is represented by the `Form1` model.

Important fields include:

- Grant ID
- Program Title
- Contracting Agency
- Contract Number
- Amendment Number
- Contract Start Date
- Contract End Date
- Contract Amount
- Funding Sources
- Federal Information Status
- Federal Grantor
- Federal ALN
- Internal Award Code
- Internal GL Start Date
- Internal GL End Date
- Program Income Treatment
- Status

The existing `status` field remains in the current application but is considered obsolete and is a candidate for future removal.

---

# 7. Grant Date Rules

Contract dates and Internal GL dates are business calendar dates.

The application validates:

1. Contract-date overlap for grants using the same Internal Award Code.
2. Internal GL-date overlap for grants using the same Internal Award Code.
3. Internal GL date relationships to the applicable grant period.

The four primary business-date fields are now Django `DateField` values:

```text
contract_start_date
contract_end_date
internal_gl_start_date
internal_gl_end_date
```

This change is implemented by migration `0025` and prevents timezone conversion from causing dates to display one calendar day earlier.

---

# 8. GL Expenditure Assignment

GL expenditure data is imported from Excel.

Important GL source columns include:

```text
Effective
Award Code
Debit
Credit
```

Derived values include:

```text
net_expenditure = Credit - Debit
```

and fiscal-year labels in the form:

```text
FYxx-xx
```

A GL transaction belongs to a GrantWise grant when:

1. the GL Award Code equals the grant's Internal Award Code; and
2. the GL Effective date falls between the grant's Internal GL Start Date and Internal GL End Date, inclusive.

This design is important because Internal Award Codes may be reused for different contracts over time.

The application therefore determines grant ownership using both the award code and the effective-date range.

---

# 9. Subsequent Adjustments

Subsequent Adjustment data is stored separately from ordinary GL expenditure data.

The application tracks:

- Subsequent Adjustment source records;
- Subsequent Federal allocation;
- Subsequent Nonfederal allocation;
- fiscal year;
- review snapshots used to determine whether a previously reviewed allocation remains current.

---

# 10. Funding Sources

The current Funding Sources classifications are:

```text
FEDERAL
NONFEDERAL
BOTH
REVIEW_REQUIRED
```

In the user interface, `REVIEW_REQUIRED` is presented as:

```text
Needs Review
```

Funding Sources affect how Federal and Nonfederal expenditure amounts are interpreted.

### Federal

For a grant classified as Federal:

```text
Federal = current expenditure total
Nonfederal = 0
```

### Nonfederal

For a grant classified as Nonfederal:

```text
Federal = 0
Nonfederal = current expenditure total
```

### Both

For a grant classified as Both:

- Federal is a stored/manual allocation;
- Nonfederal is derived from the current total less Federal;
- the allocation is considered current only if the reviewed expenditure snapshot agrees with the current accounting-source total.

### Needs Review

For grants not yet reliably classified:

- historical Federal and Nonfederal values may remain visible for reference;
- those values should not be interpreted as an authoritative Funding Sources classification.

---

# 11. Funding Source Changes

Funding Source changes do not, by themselves, change GL transaction ownership.

GL reassignment is triggered by changes to:

```text
internal_award_code
internal_gl_start_date
internal_gl_end_date
```

Changing only Funding Sources therefore does not cause GL transactions to be rematched.

Current Funding Source transition behavior is:

### Federal ↔ Nonfederal

Federal and Nonfederal amounts are automatically recalculated from current expenditure totals.

### Both → Federal or Nonfederal

The authoritative allocation becomes automatic under the new classification.

Historical allocation records may remain in the database for audit/reference purposes but are not used as the authoritative current allocation.

### Federal / Nonfederal / Needs Review → Both

Existing Federal and Nonfederal allocation values are preserved for reference.

However, allocation-review snapshots are cleared so that the Accountant must perform a fresh review against current expenditure totals.

This safeguard applies to both:

- ordinary fiscal expenditure allocation; and
- Subsequent Adjustment allocation.

---

# 12. Federal Information

Federal Information includes:

- Federal Grantor
- Federal ALN
- Federal Information Status

The application distinguishes incomplete Federal information from Funding Sources classification.

Current fiscal review labeling includes:

```text
Federal Information Incomplete
```

A Federal ALN follows the format:

```text
xx.xxx
```

---

# 13. Fiscal Review

GrantWise calculates fiscal-review status dynamically.

A grant may require fiscal review for reasons including:

```text
Funding Sources Needs Review
Federal Information Incomplete
Program Income Treatment Needs Review
Negative Contract Balance Needs Review
In-Period Allocation Needs Review
Subsequent Adjustment Allocation Needs Review
```

The fiscal-review engine returns:

- whether attention is required;
- the number of review reasons;
- the underlying reason codes.

These same rules are used in the Grant List / Grant Detail presentation and in the CSV export.

---

# 14. Fiscal Allocation Review Snapshots

For grants classified as `BOTH`, GrantWise stores review snapshots to determine whether an Accountant-approved allocation is still based on the current source accounting total.

For ordinary expenditure, the snapshot field is:

```text
reviewed_total_allowed_expenditure
```

For Subsequent Adjustments:

```text
reviewed_total_subsequent_adjustment
```

If the current accounting total later changes, the stored allocation becomes stale and the application flags the fiscal year for review.

Changing a grant into the `BOTH` classification also invalidates existing review snapshots, requiring a fresh Accountant review while preserving historical allocation amounts.

---

# 15. Program Income and Fiscal Exceptions

The current release includes Program Income treatment and fiscal-exception review structures.

Fiscal review can identify issues such as:

- unresolved Program Income treatment;
- negative contract balances;
- stale expenditure allocation;
- stale Subsequent Adjustment allocation.

Accepted exception reviews preserve the financial context against which the exception was reviewed so that a future change in the underlying numbers can trigger review again.

---

# 16. CSV Export

The CSV export has been updated to use the current Funding Sources, Federal Information, allocation, and Fiscal Review rules.

The current export contains 22 columns:

```text
Grant ID
Program Title
Contracting Agency
Contract Number
Contract Start Date
Contract End Date
Contract Amount
Funding Sources
Federal Information Status
Federal Grantor
Federal ALN
Internal Award Code
Internal GL Start Date
Internal GL End Date
Status
Source
Fiscal Year
Total Expenditure
Federal
Nonfederal
Fiscal Review
Fiscal Review Reasons
```

The former `Difference` column has been removed.

The previous `Fiscal Review Issues` field has been replaced by:

```text
Fiscal Review
Fiscal Review Reasons
```

Money values are exported with exactly two decimal places.

The existing `Status` field remains in the export for now but is expected to be reconsidered when the obsolete Form1 status field is removed.

---

# 17. Basic Information Change Request Workflow

Changes to grant Basic Information use a formal approval workflow.

The workflow supports:

- draft changes;
- submission;
- review;
- return for revision;
- resubmission;
- approval;
- denial;
- withdrawal;
- notes;
- revision history;
- audit actions.

A submitted change requires two distinct Approvers.

The person who proposed the change cannot approve their own request.

The two approvals must come from different eligible Approvers.

Before final approval, the application revalidates the proposed values against the current authoritative grant data.

---

# 18. Change Request Revisions and History

Change Request revisions are preserved as audit history.

The application stores revision-specific:

- field snapshots;
- submitter information;
- approval actions;
- returned-for-revision actions;
- resubmissions;
- withdrawals;
- notes;
- integrity information.

Revision 1 uses the original submitter.

Later revisions identify the submitter from the corresponding `RESUBMIT` action.

Historical records are read-only.

---

# 19. Coordinated Change Requests

GrantWise supports coordinated Basic Information changes involving multiple grants.

This is necessary when changing one grant cannot be validated independently because related grants must be adjusted together.

Examples include overlapping Internal Award Code / GL periods.

Coordinated change packages include:

- two or more child Change Requests;
- shared validation;
- concurrency protection;
- authoritative-data checks;
- coordinated revision handling;
- atomic final application.

If final validation fails, the package does not partially update the affected grants.

All grant changes in the coordinated package are applied atomically.

GL rematching occurs after all coordinated grant changes have been applied so that temporary intermediate conflicts do not cause incorrect ownership.

---

# 20. User Roles

GrantWise includes the following application roles:

### Administrator

May perform administrative user-management functions and exercise application-level administrative privileges.

### Viewer

May navigate application pages and view/download data.

### Editor

May propose Basic Information changes and create new grant requests.

### Accountant

May refresh accounting-source data and maintain fiscal allocations that do not require the Basic Information approval workflow.

### Approver

May review and act on submitted Basic Information Change Requests and New Grant Requests.

A user may belong to more than one role.

Role groups are created through migration `0009`.

---

# 21. Automated Regression Testing

The current development release includes automated regression coverage for major recent changes.

The current `tracking` test suite contains 18 tests.

The suite was run successfully before the dependency-only cleanup commit:

```text
18 tests
18 passed
```

Additional checks completed successfully included:

```powershell
python -m py_compile
python award_tracking_web/manage.py check
git diff --check
```

The later commit `b71ea97` changed only dependency-file organization and encoding.

That requirements change was separately validated with:

```powershell
python -m pip install --dry-run --no-index -r requirements.txt
python -m pip check
```

No application source code was changed by that commit.

---

# 22. New Tracking Migrations in This Release

The current release adds the following migrations beyond Phase One:

| Migration | Purpose | Classification |
|---|---|---|
| `0007` | Add workflow models and Federal ALN validation support | Schema |
| `0008` | Add grant document models | Schema |
| `0009` | Create application user-role groups | Application/setup data |
| `0010` | Add grant classification fields | Schema |
| `0011` | Classify existing Federal Information | Existing-data migration |
| `0012` | Normalize Federal placeholder values | Existing-data cleanup |
| `0013` | Add Funding Sources | Schema |
| `0014` | Clean specifically identified orphan allocation records | Targeted data cleanup |
| `0015` | Classify existing Funding Sources | Existing-data migration |
| `0016` | Add allocation review snapshots | Schema |
| `0017` | Initialize valid allocation review snapshots | Existing-data initialization |
| `0018` | Add Program Income and fiscal-exception review | Schema |
| `0019` | Add active Change Request uniqueness constraint | Schema / constraint |
| `0020` | Add coordinated-change workflow structures | Schema |
| `0021` | Add coordinated cancellation/review fields | Schema |
| `0022` | Add Change Request integrity structures | Schema |
| `0023` | Add coordinated revision structures | Schema |
| `0024` | Add New Grant Request uniqueness constraint | Schema / constraint |
| `0025` | Convert four Form1 DateTime fields to Date fields | Schema + data conversion |

---

# 23. Migration 0011 – Federal Information Classification

Migration `0011` initializes Federal Information status from existing production data.

It treats the following values as missing:

```text
blank
none
n/a
na
not applicable
```

Classification is:

### Complete

Federal Grantor exists and Federal ALN is valid in `xx.xxx` format.

### Not Applicable

Neither Federal Grantor nor Federal ALN is meaningfully present.

### Pending

Any other combination.

The migration prints counts for:

```text
Complete
Pending
Not Applicable
Total classified
```

IT should retain this migration output as part of the production deployment record.

---

# 24. Migration 0012 – Federal Placeholder Cleanup

Migration `0012` converts recognized placeholder text in:

```text
Federal Grantor
Federal ALN
```

to database `NULL`.

Recognized placeholder values include:

```text
blank
none
n/a
na
not applicable
```

The migration changes no other values.

It prints counts for:

- Federal Grantor values cleared;
- Federal ALN values cleared;
- records updated.

The reverse migration is a no-op. Therefore, the original placeholder spelling cannot be reconstructed by reversing the migration.

A production backup must exist before migration.

---

# 25. Migration 0014 – Targeted Orphan Allocation Cleanup

Migration `0014` is intentionally narrow.

It considers only four identified historical allocation candidates:

```text
A00073 / FY22-23 / Federal 96600.00 / Nonfederal 0.00
A00028 / FY22-23 / Federal 0.00 / Nonfederal 0.00
A00029 / FY22-23 / Federal 0.00 / Nonfederal 0.00
A00062 / FY22-23 / Subsequent / Federal -6359.00 / Nonfederal 0.00
```

A row is deleted only when:

1. no corresponding GL or Subsequent Adjustment source exists for the Grant ID / fiscal year; and
2. exactly one allocation row matches the expected Grant ID, fiscal year, Federal amount, and Nonfederal amount.

The migration skips deletion when:

- matching source accounting data exists;
- the exact target row is not present; or
- multiple exact matches are present.

This migration is non-reversible. Its reverse operation is `RunPython.noop`.

The production database backup is therefore particularly important before applying this migration.

---

# 26. Migration 0015 – Funding Sources Classification

Migration `0015` initializes Funding Sources from existing grant and allocation information.

It considers:

- historical Federal allocation;
- historical Nonfederal allocation;
- Federal Grantor;
- whether the Contracting Agency begins with `U.S. Department of`.

Classification behavior is intentionally conservative.

### Federal

A direct Federal contracting agency is classified Federal.

### Nonfederal

If there is no Federal Grantor and no historical Federal allocation, the grant is classified Nonfederal.

### Both

If a Federal Grantor exists and historical Nonfederal allocation exists, the grant is classified Both.

### Review Required

Ambiguous cases are assigned `REVIEW_REQUIRED`.

In particular, the migration does not assume that a grant is fully Federal merely because a Federal Grantor or historical Federal allocation exists.

The migration prints counts for:

```text
FEDERAL
NONFEDERAL
BOTH
REVIEW_REQUIRED
TOTAL
```

IT should retain these totals for post-deployment review.

---

# 27. Migration 0017 – Allocation Review Snapshot Initialization

Migration `0017` operates only on grants classified `BOTH`.

For each fiscal-year source total, it attempts to identify one corresponding allocation row.

The review snapshot is initialized only when:

1. exactly one allocation row exists; and
2. Federal + Nonfederal equals the current source accounting total.

The migration deliberately does not initialize a snapshot when:

- no allocation row exists;
- duplicate allocation rows exist; or
- the allocation does not reconcile to the accounting-source total.

The same logic applies separately to:

- ordinary GL expenditures; and
- Subsequent Adjustments.

The migration prints:

```text
Initialized
Source without breakdown
Allocation mismatch
Duplicate breakdown
```

for both categories.

The migration does not change the Federal or Nonfederal allocation amounts.

Its reverse function clears all allocation-review snapshots.

For that reason, IT should not casually roll production back through this migration after users have begun performing allocation reviews, because later user-created review snapshots would also be cleared.

---

# 28. Migration 0025 – Business Date Conversion

Migration `0025` converts these fields from DateTime storage to Date-only storage:

```text
contract_start_date
contract_end_date
internal_gl_start_date
internal_gl_end_date
```

The migration performs the conversion in stages:

1. adds four temporary `DateField` columns;
2. copies the stored calendar-date portion into those temporary fields;
3. removes the former DateTime fields;
4. renames the temporary fields to the original field names.

The migration intentionally does not convert the stored DateTime value into local timezone before taking its date.

For example:

```text
2025-03-01 00:00:00+00:00
```

becomes:

```text
2025-03-01
```

This prevents a timezone conversion from incorrectly displaying the business date as the previous calendar day.

The migration has been tested successfully in development.

IT should nevertheless compare these four fields before and after production migration.

---

# 29. Required Production Upgrade Procedure

The following procedure is recommended for upgrading the existing Phase One installation.

## Step 1 – Establish a Maintenance Window

Application writes should be stopped during the database migration.

Users should not be editing grants, refreshing accounting data, approving change requests, or creating new records while the database structure is being upgraded.

## Step 2 – Confirm the Release

Confirm the intended branch/release:

```text
new-grant-approval-workflow
```

Current checkpoint:

```text
b71ea97
```

## Step 3 – Update the Application Code

Update the application files from the approved repository release.

Do not overwrite the production database with a development database.

## Step 4 – Update Python Dependencies

From the repository root:

```powershell
python -m pip install -r requirements.txt
```

## Step 5 – Confirm Existing Migration State

Before running any new migration:

```powershell
python award_tracking_web/manage.py showmigrations tracking
```

The expected Phase One baseline is through:

```text
tracking.0006
```

If the production database shows a materially different migration history, stop and investigate before continuing.

Do not use `--fake` unless the actual database schema and migration history have been independently verified.

## Step 6 – Create a Complete Production Database Backup

The production database must be backed up immediately before migration.

The backup should be preserved until post-deployment validation is complete.

For SQLite, the safest practice is to stop application writes before copying/backing up the database file.

## Step 7 – Retain Pre-Migration Validation Data

Before migration, retain enough information to validate the production data afterward.

At minimum, capture:

- grant count;
- Federal Grantor / Federal ALN information;
- fiscal allocation records;
- Subsequent Adjustment allocations;
- the four Form1 business-date fields;
- current Django migration state.

## Step 8 – Review the Migration Plan

Run:

```powershell
python award_tracking_web/manage.py migrate --plan
```

Expected new `tracking` migrations:

```text
0007 through 0025
```

## Step 9 – Prefer a Staging Migration First

Where practical, restore a copy of the production backup into a test/staging environment and apply the release there first.

This is particularly valuable because several migrations classify or normalize existing production records.

## Step 10 – Apply Migrations

Run:

```powershell
python award_tracking_web/manage.py migrate
```

Capture the complete console output.

The output from migrations `0011`, `0012`, `0014`, `0015`, and `0017` should be retained for review.

## Step 11 – Confirm Migration Completion

Run:

```powershell
python award_tracking_web/manage.py showmigrations tracking
```

All migrations through:

```text
0025
```

should show as applied.

## Step 12 – Run Django System Check

Run:

```powershell
python award_tracking_web/manage.py check
```

Expected result:

```text
System check identified no issues
```

## Step 13 – Perform Data Validation

Compare production data with the pre-migration validation information.

Particular attention should be given to:

- grant count;
- Federal Grantor;
- Federal ALN;
- Federal Information Status;
- Funding Sources;
- fiscal allocations;
- Subsequent Adjustment allocations;
- contract dates;
- Internal GL dates.

## Step 14 – Review Fiscal Classification Results

Review the migration totals from:

```text
0011
0015
0017
```

Records routed to:

```text
PENDING
REVIEW_REQUIRED
allocation mismatch
duplicate breakdown
```

are expected to require human review rather than automatic correction.

## Step 15 – Application Smoke Test

Confirm at minimum:

- login works;
- Grant List loads;
- Grant Detail loads;
- dates display correctly;
- CSV export works;
- fiscal information displays;
- Change Request pages load;
- role-based queues load;
- Accountant functions are accessible to authorized users;
- approval functions are accessible to authorized users.

## Step 16 – Resume Normal Application Use

Normal user access should resume only after migration and validation are complete.

---

# 30. Rollback Considerations

The preferred rollback mechanism is restoration of the pre-migration production backup.

A database backup is safer than relying on Django migration reversal because some data migrations are intentionally non-reversible or have broad reverse behavior.

Important examples:

### Migration 0012

Placeholder cleanup cannot reconstruct the original placeholder text.

### Migration 0014

Deleted orphan allocation rows are not recreated by reversing the migration.

### Migration 0017

Reversing clears review snapshots globally.

### Migration 0025

Although a reverse operation exists, restoring the verified pre-migration database backup is preferable if deployment must be abandoned.

Therefore, database backup and controlled restoration should be considered the primary rollback strategy.

---

# 31. Production Database Is the Source of Truth

The company-server production database is the authoritative GrantWise data source.

Development and local testing databases are not authoritative.

A corrected development record must not be assumed to update production automatically.

Production corrections require:

- an application workflow;
- a production migration; or
- a separately controlled production-data correction approved by IT.

---

# 32. Current Source-Control Checkpoints

Important recent checkpoints include:

```text
9c9334b  Phase One / master baseline
3cde8da  Update CSV export and funding source allocation workflow
b71ea97  Consolidate Python deployment requirements
```

The current branch is:

```text
new-grant-approval-workflow
```

At the final development checkpoint, the branch had been pushed successfully to the remote repository and the working tree was clean.

---

# 33. Known Deferred or Future Work

The following items are not deployment blockers but remain candidates for future cleanup or enhancement:

### Form1 Status

The legacy `status` field is considered obsolete and may be removed in a later release.

### Fiscal Information Edit UI

The placement/presentation of the Fiscal Information Edit control may be improved.

### Minor Template Text Encoding

A small text-encoding/mojibake issue remains in the New Grant Requests template.

### Additional Documentation

End-user documentation and a full Getting Started section can be expanded separately from this IT handoff.

### Continued Regression Coverage

The automated regression suite should continue to grow as additional production workflows are finalized.

---

# 34. Recommended IT Review Areas

Before production deployment, IT should specifically review:

1. Python virtual-environment setup and the root `requirements.txt`.
2. Production database backup and restoration procedure.
3. Existing production Django migration history.
4. Migration plan from `0007` through `0025`.
5. Data migrations `0011`, `0012`, `0014`, `0015`, `0017`, and `0025`.
6. Authentication and user-role assignments.
7. Application paths and filesystem permissions.
8. Accounting-data upload locations and permissions.
9. Database-file permissions and backup controls.
10. Post-deployment verification procedure.

---

# 35. Recommended Deployment Evidence to Retain

For auditability and troubleshooting, retain the following with the deployment record:

- deployed Git commit;
- deployment date/time;
- person performing deployment;
- production database backup identifier;
- pre-migration `showmigrations` output;
- `migrate --plan` output;
- complete `migrate` output;
- migration classification totals;
- post-migration `showmigrations` output;
- `manage.py check` output;
- post-migration validation results;
- confirmation that business dates remained unchanged;
- any production records requiring follow-up review.

---

# 36. Final Deployment Principle

The central deployment principle for this release is:

> **Preserve the existing production database, verify its Phase One migration state, back it up, and allow Django migrations `0007` through `0025` to upgrade it in sequence. Do not replace production with the development SQLite database.**

The migrations were designed to preserve existing accounting and grant information wherever possible and to route ambiguous classifications or unreconciled allocations to human review rather than silently guessing.

This approach allows the existing Phase One GrantWise installation to be upgraded while maintaining the integrity and auditability of the organization's grant data.
