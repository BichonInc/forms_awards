from django.conf import settings
from django.db import migrations


ROLE_NAMES = [
    "Administrator",
    "Viewer",
    "Editor",
    "Accountant",
    "Approver",
]


def create_user_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")

    for role_name in ROLE_NAMES:
        Group.objects.get_or_create(name=role_name)


class Migration(migrations.Migration):

    dependencies = [
        ("tracking", "0008_add_grant_document_models"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            create_user_groups,
            migrations.RunPython.noop,
        ),
    ]