from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


ROLE_ADMINISTRATOR = "Administrator"
ROLE_VIEWER = "Viewer"
ROLE_EDITOR = "Editor"
ROLE_ACCOUNTANT = "Accountant"
ROLE_APPROVER = "Approver"


def user_has_any_role(user, *role_names):
    """
    Return True when the user has any requested role.

    Superusers and members of the Administrator Group
    receive access to every role-protected function.
    """
    if not user.is_authenticated:
        return False

    if user.is_superuser:
        return True

    if user.groups.filter(
        name=ROLE_ADMINISTRATOR,
    ).exists():
        return True

    return user.groups.filter(
        name__in=role_names,
    ).exists()


def role_required(*role_names):
    """
    Require authentication and at least one requested role.

    Unauthenticated users are redirected to the login page.
    Authenticated users without permission receive HTTP 403.
    """
    if not role_names:
        raise ValueError(
            "role_required() needs at least one role."
        )

    def decorator(view_function):
        @wraps(view_function)
        @login_required
        def wrapped_view(request, *args, **kwargs):
            if not user_has_any_role(
                request.user,
                *role_names,
            ):
                raise PermissionDenied

            return view_function(
                request,
                *args,
                **kwargs,
            )

        return wrapped_view

    return decorator