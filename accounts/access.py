"""
Cross-institute access rules.

Only platform users (platform_owner, manager) work across institutes.
Only platform users (platform_owner) work across institutes.
Everyone else -- super_admin included, who owns ONE institute -- is limited
to their primary institute plus institutes they hold an active membership
in. Role checks ("is this user an admin?") stay in the views; this module
answers "is this object in an institute the user may touch?".

Most institutes share the default database, so tenant DB routing alone does
not isolate them: querysets must be filtered by institute too.
"""
from django.db.models import Q

from .subdomains import is_platform_user

ADMIN_ROLES = frozenset({'super_admin', 'institute_admin', 'exam_admin', 'admin'})

# Who may grant which role: an actor may assign roles ranked at or below
# their own. Platform roles (platform_owner, manager -- same access) can only
# be granted by platform users.
ROLE_RANK = {
    'student': 1, 'teacher': 2, 'staff': 2,
    'admin': 3, 'exam_admin': 3, 'institute_admin': 4,
    'super_admin': 5, 'manager': 6, 'platform_owner': 6,
}


def _role(user):
    return (getattr(user, 'role', '') or '').lower()


def institute_ids_for(user, write=False):
    """
    Institutes `user` may act in, or None for "all" (platform users).
    For writes, memberships only count when they carry an admin role.
    """
    if is_platform_user(user):
        return None
    from .models import UserInstituteMembership
    ids = {user.institute_id} if user.institute_id else set()
    memberships = UserInstituteMembership.objects.using('default').filter(user_id=user.pk, is_active=True)
    if write:
        memberships = memberships.filter(role__in=ADMIN_ROLES)
    ids.update(memberships.values_list('institute_id', flat=True))
    return ids


def can_access_institute(user, institute_id, write=False):
    allowed = institute_ids_for(user, write=write)
    if allowed is None:
        return True
    try:
        return int(institute_id) in allowed
    except (TypeError, ValueError):
        return False


def scope_to_institutes(queryset, user, *lookups):
    """
    Restrict `queryset` to the user's institutes. `lookups` are paths to an
    institute id, OR-ed together, e.g. ('center__institute_id', 'program__institute_id').
    """
    allowed = institute_ids_for(user)
    if allowed is None:
        return queryset
    condition = Q()
    for lookup in lookups or ('institute_id',):
        condition |= Q(**{f'{lookup}__in': allowed})
    return queryset.filter(condition)


def can_assign_role(actor, role):
    """Whether `actor` may give someone the role `role`."""
    role = (role or '').lower()
    if role not in ROLE_RANK:
        return False
    if is_platform_user(actor):
        return True
    if role in ('platform_owner', 'manager'):
        return False
    actor_rank = ROLE_RANK.get(_role(actor), 0)
    return actor_rank >= ROLE_RANK['admin'] and ROLE_RANK[role] <= actor_rank


def can_change_role(actor, target, new_role):
    """
    Role changes on an existing user: nobody re-ranks themselves (except
    platform users), and an actor cannot re-rank someone who outranks them.
    """
    if is_platform_user(actor):
        return (new_role or '').lower() in ROLE_RANK
    if actor.pk == target.pk:
        return False
    if ROLE_RANK.get(_role(target), 0) > ROLE_RANK.get(_role(actor), 0):
        return False
    return can_assign_role(actor, new_role)
