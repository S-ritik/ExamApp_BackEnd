"""
Institute subdomain rules: normalisation, validation, host parsing and
membership checks. Shared by the model, serializers, TenantMiddleware and
the login views so they all agree on what a subdomain is.

  kiit.exams.dashoapp.com  -> tenant 'kiit'
  exams.dashoapp.com       -> root portal (no tenant)
  www.exams.dashoapp.com   -> root portal (no tenant)
  anything else            -> no tenant (localhost, IP, timetable domain, ...)

In DEBUG, '<label>.localhost' is also treated as a tenant host so the flow
can be exercised locally (browsers resolve *.localhost to loopback).
"""
import re

from django.conf import settings
from django.core.exceptions import ValidationError

DEFAULT_ROOT_DOMAIN = 'exams.dashoapp.com'
DEV_ROOT_DOMAIN = 'localhost'

# Labels under the root domain that mean "the root portal itself"
ROOT_ALIASES = frozenset({'www'})

# Labels that can never be assigned to an institute
RESERVED_SUBDOMAINS = frozenset({
    'www', 'api', 'app', 'apps', 'admin', 'administrator', 'root', 'platform',
    'owner', 'superadmin', 'dashboard', 'portal', 'auth', 'login', 'logout',
    'signin', 'signup', 'register', 'sso', 'oauth', 'account', 'accounts',
    'mail', 'email', 'webmail', 'smtp', 'imap', 'pop', 'mx', 'ns', 'ns1', 'ns2',
    'ftp', 'sftp', 'ssh', 'vpn', 'git', 'cdn', 'static', 'media', 'assets',
    'files', 'img', 'images', 'exam', 'exams', 'timetable', 'dashoapp',
    'billing', 'payments', 'help', 'support', 'docs', 'blog', 'status',
    'health', 'metrics', 'monitor', 'internal', 'dev', 'staging', 'test',
    'qa', 'beta', 'localhost',
})

MIN_LENGTH = 2
MAX_LENGTH = 63  # DNS label limit
_FORMAT_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$')

# Roles that operate across institutes (not tied to one tenant)
PLATFORM_ROLES = frozenset({'platform_owner', 'manager'})


def normalize_subdomain(value):
    """Trim and lowercase; empty values become None (= no subdomain)."""
    if value is None:
        return None
    value = str(value).strip().lower()
    return value or None


def subdomain_error(value):
    """Return a human-readable reason `value` is not a valid subdomain, or None."""
    if not MIN_LENGTH <= len(value) <= MAX_LENGTH:
        return f'Subdomain must be between {MIN_LENGTH} and {MAX_LENGTH} characters.'
    if not _FORMAT_RE.match(value):
        return ('Subdomain may only contain lowercase letters, numbers and hyphens, '
                'and must start and end with a letter or number.')
    if '--' in value:
        return 'Subdomain cannot contain consecutive hyphens.'
    if value in RESERVED_SUBDOMAINS:
        return f'"{value}" is reserved and cannot be used as a subdomain.'
    return None


def validate_subdomain(value):
    """Model/serializer validator. Expects an already-normalised value."""
    if value is None:
        return
    error = subdomain_error(value)
    if error:
        raise ValidationError(error, code='invalid_subdomain')


def _root_domains():
    root = getattr(settings, 'ROOT_DOMAIN', DEFAULT_ROOT_DOMAIN).lower()
    return (root, DEV_ROOT_DOMAIN) if settings.DEBUG else (root,)


def subdomain_from_host(host):
    """
    Return the tenant label for `host`, or None when the host is not a tenant
    host. The label is NOT validated: an unregistered/reserved/nested label
    is still returned so the caller can reject it.
    """
    host = (host or '').split(':')[0].strip().lower().rstrip('.')
    for root in _root_domains():
        suffix = '.' + root
        if host.endswith(suffix):
            label = host[:-len(suffix)]
            if not label or label in ROOT_ALIASES:
                return None
            return label
    return None


def resolve_institute(label):
    """Active institute registered under `label`, or None."""
    if not label or subdomain_error(label):
        return None
    from .models import Institute
    return Institute.objects.using('default').filter(subdomain=label, is_active=True).first()


def subdomain_is_taken(value, exclude_pk=None):
    from .models import Institute
    qs = Institute.objects.using('default').filter(subdomain__iexact=value)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def user_belongs_to_institute(user, institute):
    """Primary institute or an active membership. Memberships are read from the shared DB."""
    if not (user and user.is_authenticated and institute):
        return False
    if user.institute_id == institute.pk:
        return True
    from .models import UserInstituteMembership
    return UserInstituteMembership.objects.using('default').filter(
        user_id=user.pk, institute_id=institute.pk, is_active=True
    ).exists()


def is_platform_user(user):
    return (getattr(user, 'role', '') or '').lower() in PLATFORM_ROLES


def portal_access_denied_payload(institute):
    return {
        'detail': f'Your account does not have access to the {institute.name} portal.',
        'code': 'subdomain_access_denied',
    }


def authorize_portal_login(request, user):
    """
    For login views, after the password/token check. Returns a 403 Response
    if `user` may not sign in on the institute portal this request came
    through. Otherwise returns None and points the rest of the request at the
    right tenant DB (the portal's institute, else the user's own), so tenant
    lookups in the login response (center, ...) hit the right database.

    Off-subdomain (root portal) logins are not restricted, so institutes
    without a subdomain keep working.
    """
    institute = getattr(request, 'institute', None)
    if institute is not None and not user_belongs_to_institute(user, institute):
        from rest_framework import status
        from rest_framework.response import Response
        return Response(portal_access_denied_payload(institute), status=status.HTTP_403_FORBIDDEN)

    from .tenant_middleware import activate_tenant_db
    target = institute or user.institute
    activate_tenant_db(target.db_name if target else None)
    return None
