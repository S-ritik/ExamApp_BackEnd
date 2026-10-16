from django.utils.deprecation import MiddlewareMixin
from django.http import JsonResponse
from accounts.utils import set_current_db, clear_current_db
from accounts.subdomains import (
    subdomain_from_host, resolve_institute, user_belongs_to_institute,
    is_platform_user, portal_access_denied_payload,
)
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class TenantMiddleware(MiddlewareMixin):
    """
    1. Resolves the institute from the request host (accounts/subdomains.py).
       kiit.exams.dashoapp.com -> request.institute = Institute(subdomain='kiit').
       A tenant-style host with no active institute behind it gets a 404 —
       unknown subdomains never fall through to the generic portal.
    2. On an institute subdomain, an authenticated user must belong to that
       institute (403 otherwise) and the request is pinned to its database.
    3. Elsewhere (root domain, localhost, ...), the user's own institute DB is
       used; X-Institute-DB may switch to another DB only if the user is a
       member of that institute (or a platform-level user).
    """

    # Login/registration endpoints check the institute themselves (against
    # request.institute). A stale token in the browser must not block them.
    PUBLIC_AUTH_PATHS = (
        '/api/auth/login',
        '/api/auth/logout',
        '/api/auth/register',
        '/api/auth/token/refresh',
        '/api/auth/google',
        '/api/auth/institutes/by-subdomain/',
        '/api/timetable/auth/',
    )

    def process_request(self, request):
        set_current_db('default')
        request.institute = None
        request.subdomain = None

        label = subdomain_from_host(request.get_host())
        if label is not None:
            institute = resolve_institute(label)
            if institute is None:
                logger.warning(f"[Tenant] Rejected request for unknown subdomain: '{label}'")
                return JsonResponse({
                    'detail': 'No institute portal exists at this address.',
                    'code': 'unknown_subdomain',
                }, status=404)
            request.institute = institute
            request.subdomain = institute.subdomain

        user = self._get_user(request)

        if request.institute is not None:
            if (user is not None
                    and not request.path.startswith(self.PUBLIC_AUTH_PATHS)
                    and not user_belongs_to_institute(user, request.institute)):
                logger.warning(
                    f"[Tenant] {user.email} denied on '{request.subdomain}' portal (not a member)"
                )
                return JsonResponse(portal_access_denied_payload(request.institute), status=403)
            # The subdomain decides the tenant; X-Institute-DB is ignored here.
            return self._activate(request.institute.db_name)

        if user is None:
            # Anonymous requests off-subdomain only ever see the shared DB.
            return self._activate(None)

        requested_db = request.headers.get('X-Institute-DB')
        if not requested_db:
            return self._activate(user.institute.db_name if user.institute else None)
        if self._may_use_db(user, requested_db):
            return self._activate(requested_db)

        logger.warning(f"User {user.email} attempted to access unauthorized database: {requested_db}")
        return JsonResponse({
            'detail': 'You do not have access to this institute.',
            'code': 'institute_access_denied',
        }, status=403)

    def process_response(self, request, response):
        """Clear the thread-local storage on response."""
        clear_current_db()
        return response

    @staticmethod
    def _get_user(request):
        """Authenticated user from the session or a valid JWT, else None."""
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            return user
        try:
            jwt_auth = JWTAuthentication()
            header = jwt_auth.get_header(request)
            if header:
                raw_token = jwt_auth.get_raw_token(header)
                if raw_token:
                    user = jwt_auth.get_user(jwt_auth.get_validated_token(raw_token))
                    if user.is_authenticated and user.is_active:
                        return user
        except Exception:
            pass
        return None

    @staticmethod
    def _may_use_db(user, db_name):
        from .models import Institute
        if is_platform_user(user):
            return db_name == 'default' or Institute.objects.using('default').filter(db_name=db_name).exists()
        if db_name == 'default':
            # The shared DB also holds data of institutes without their own DB.
            return not (user.institute and user.institute.db_name)
        target = Institute.objects.using('default').filter(db_name=db_name).first()
        return target is not None and user_belongs_to_institute(user, target)

    @staticmethod
    def _activate(tenant_db):
        activate_tenant_db(tenant_db)
        return None


def activate_tenant_db(tenant_db):
    """Route this thread's tenant-model queries to `tenant_db` (None/'default' = shared DB)."""
    if not tenant_db or tenant_db == 'default':
        set_current_db('default')
        return

    if tenant_db not in settings.DATABASES:
        from .models import Institute
        institute = Institute.objects.using('default').filter(db_name=tenant_db).first()
        if institute is None:
            logger.error(f"Tenant database {tenant_db} requested but no such institute found.")
            set_current_db('default')
            return
        from .database_utils import register_institute_database
        register_institute_database(institute)

    set_current_db(tenant_db)
