"""
Institute subdomains: validation, host routing, portal login, tenant DB
selection and cross-institute isolation.

    python manage.py test accounts --settings=exam_flow_backend.test_settings
"""
import importlib
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import Institute, InstituteInvitation, User, UserInstituteMembership
from accounts.subdomains import (
    normalize_subdomain, subdomain_error, subdomain_from_host, validate_subdomain,
)
from accounts.tenant_middleware import TenantMiddleware
from accounts.utils import clear_current_db, get_current_db

ROOT = 'exams.dashoapp.com'
cleanup_migration = importlib.import_module('accounts.migrations.0019_normalize_institute_subdomains')


def host(label=None):
    return f'{label}.{ROOT}' if label else ROOT


def bearer(user):
    return f'Bearer {RefreshToken.for_user(user).access_token}'


class SubdomainRulesTests(SimpleTestCase):
    def test_accepts_valid_subdomains(self):
        for value in ['hd', 'kiit', 'iit-madras', 'abc123', '1stschool', 'a' * 63]:
            with self.subTest(value=value):
                self.assertIsNone(subdomain_error(value))
                validate_subdomain(value)

    def test_rejects_invalid_and_reserved_subdomains(self):
        for value in ['a', 'a' * 64, '-kiit', 'kiit-', 'ki_it', 'ki.it', 'ki it', 'mx', 'qa',
                      'KIIT', 'xn--kiit', 'kii--t', 'www', 'api', 'admin', 'timetable']:
            with self.subTest(value=value):
                self.assertIsNotNone(subdomain_error(value))
                with self.assertRaises(ValidationError):
                    validate_subdomain(value)

    def test_normalize(self):
        self.assertEqual(normalize_subdomain('  KIIT '), 'kiit')
        self.assertIsNone(normalize_subdomain(''))
        self.assertIsNone(normalize_subdomain('   '))
        self.assertIsNone(normalize_subdomain(None))

    def test_host_parsing(self):
        cases = {
            'kiit.exams.dashoapp.com': 'kiit',
            'KIIT.Exams.DashoApp.com:443': 'kiit',
            'kiit.exams.dashoapp.com.': 'kiit',
            'a.b.exams.dashoapp.com': 'a.b',      # returned so it can be rejected
            'exams.dashoapp.com': None,
            'www.exams.dashoapp.com': None,
            'evilexams.dashoapp.com': None,
            'timetable.dashoapp.com': None,
            'localhost': None,
            '127.0.0.1': None,
            '128.199.17.132:8000': None,
        }
        for value, expected in cases.items():
            with self.subTest(host=value):
                self.assertEqual(subdomain_from_host(value), expected)

    def test_localhost_subdomains_only_in_debug(self):
        with override_settings(DEBUG=True):
            self.assertEqual(subdomain_from_host('kiit.localhost:5173'), 'kiit')
        with override_settings(DEBUG=False):
            self.assertIsNone(subdomain_from_host('kiit.localhost:5173'))

    def test_cleanup_migration_plan(self):
        rows = [(1, 'KIIT'), (2, ''), (3, 'kiit'), (4, 'bad_name'), (5, 'ok-one'), (6, ' Mit ')]
        changes = cleanup_migration.cleaned_subdomains(rows)
        self.assertEqual(
            {pk: value for pk, value, _ in changes},
            {1: 'kiit', 2: None, 3: None, 4: None, 6: 'mit'},
        )
        reasons = {pk: reason for pk, _, reason in changes}
        self.assertEqual(reasons[3], 'duplicate')
        self.assertIsNotNone(reasons[4])
        self.assertIsNone(reasons[2])
        # Clears are applied before renames so row 1 -> 'kiit' never hits row 3's value
        cleared = [i for i, c in enumerate(changes) if c[1] is None]
        renamed = [i for i, c in enumerate(changes) if c[1] is not None]
        self.assertLess(max(cleared), min(renamed))


class SubdomainModelTests(TestCase):
    def test_save_normalizes_and_blank_means_none(self):
        inst = Institute.objects.create(name='KIIT', subdomain='  KIIT ')
        self.assertEqual(inst.subdomain, 'kiit')
        blank = Institute.objects.create(name='Blank', subdomain='')
        self.assertIsNone(blank.subdomain)

    def test_many_institutes_without_subdomain(self):
        for i in range(3):
            Institute.objects.create(name=f'No Sub {i}', subdomain='')
        self.assertEqual(Institute.objects.filter(subdomain__isnull=True).count(), 3)

    def test_db_rejects_unnormalized_values(self):
        Institute.objects.create(name='A', subdomain='alpha')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Institute.objects.filter(name='A').update(subdomain='ALPHA')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Institute.objects.filter(name='A').update(subdomain='')

    def test_case_insensitive_uniqueness(self):
        Institute.objects.create(name='A', subdomain='alpha')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Institute.objects.create(name='B', subdomain='ALPHA')


class PortalFixtureMixin:
    """Institute A (subdomain 'alpha', own DB), B (subdomain 'bravo'), C (no subdomain)."""

    def setUp(self):
        cache.clear()  # login throttles
        clear_current_db()
        self.a = Institute.objects.create(name='Alpha', subdomain='Alpha', db_name='exam_flow_inst_a')
        self.b = Institute.objects.create(name='Beta', subdomain='bravo', db_name='exam_flow_inst_b')
        self.c = Institute.objects.create(name='Gamma')
        self.inactive = Institute.objects.create(name='Closed', subdomain='closed', is_active=False)
        self.alice = self.make_user('alice', self.a, role='student')
        self.adam = self.make_user('adam', self.a, role='super_admin')
        self.bob = self.make_user('bob', self.b, role='student')
        self.bella = self.make_user('bella', self.b, role='super_admin')
        self.carol = self.make_user('carol', self.c, role='teacher')
        self.owner = self.make_user('owner', None, role='platform_owner')

    def make_user(self, name, institute, role):
        return User.objects.create_user(
            username=name, email=f'{name}@example.com', password='pass12345',
            role=role, institute=institute,
        )

    def login(self, name, label=None, path='/api/auth/login/'):
        return self.client.post(
            path, {'email': f'{name}@example.com', 'password': 'pass12345'},
            content_type='application/json', HTTP_HOST=host(label),
        )


# Tenant DBs are not real here: registering one is a no-op, and the router sends
# every query to the test DB. Routing decisions are asserted via get_current_db().
fake_tenant_dbs = [
    mock.patch('accounts.database_utils.register_institute_database', lambda inst: inst.db_name),
    mock.patch('accounts.router.get_current_db', lambda: 'default'),
]


def with_fake_tenant_dbs(cls):
    for patcher in fake_tenant_dbs:
        cls = patcher(cls)
    return cls


@with_fake_tenant_dbs
class SubdomainRoutingTests(PortalFixtureMixin, TestCase):
    def test_unknown_subdomain_is_rejected(self):
        for label in ['nope', 'www2', 'a.b', 'api', 'ab', 'x', 'closed']:
            with self.subTest(label=label):
                res = self.client.get('/api/auth/institutes/', HTTP_HOST=host(label))
                self.assertEqual(res.status_code, 404)
                self.assertEqual(res.json()['code'], 'unknown_subdomain')

    def test_unknown_subdomain_blocks_login(self):
        res = self.login('alice', 'nope')
        self.assertEqual(res.status_code, 404)
        self.assertNotIn('tokens', res.json())

    def test_registered_subdomain_is_served(self):
        res = self.client.get('/api/auth/institutes/by-subdomain/alpha/', HTTP_HOST=host('alpha'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['name'], 'Alpha')

    def test_root_and_www_are_not_tenants(self):
        for h in [ROOT, f'www.{ROOT}', 'localhost']:
            with self.subTest(host=h):
                res = self.client.get('/api/auth/institutes/by-subdomain/bravo/', HTTP_HOST=h)
                self.assertEqual(res.status_code, 200)

    def test_by_subdomain_lookup(self):
        self.assertEqual(self.client.get('/api/auth/institutes/by-subdomain/ALPHA/').json()['subdomain'], 'alpha')
        res = self.client.get('/api/auth/institutes/by-subdomain/nope/')
        self.assertEqual(res.status_code, 404)
        self.assertNotIn('debug_all_institutes', res.json())  # used to list every institute
        self.assertEqual(self.client.get('/api/auth/institutes/by-subdomain/closed/').status_code, 404)

    def run_middleware(self, label=None, user=None, path='/api/exams/', **headers):
        request = RequestFactory().get(path, HTTP_HOST=host(label), **headers)
        if user is not None:
            request.META['HTTP_AUTHORIZATION'] = bearer(user)
        response = TenantMiddleware(lambda r: None).process_request(request)
        return request, response, get_current_db()

    def test_subdomain_pins_tenant_db(self):
        request, response, db = self.run_middleware('alpha', self.alice)
        self.assertIsNone(response)
        self.assertEqual(request.institute, self.a)
        self.assertEqual(db, 'exam_flow_inst_a')

    def test_subdomain_ignores_db_header(self):
        _, response, db = self.run_middleware('alpha', self.alice, HTTP_X_INSTITUTE_DB='exam_flow_inst_b')
        self.assertIsNone(response)
        self.assertEqual(db, 'exam_flow_inst_a')

    def test_anonymous_on_subdomain_uses_that_institute(self):
        _, response, db = self.run_middleware('bravo')
        self.assertIsNone(response)
        self.assertEqual(db, 'exam_flow_inst_b')

    def test_anonymous_cannot_pick_db_with_header(self):
        _, response, db = self.run_middleware(None, HTTP_X_INSTITUTE_DB='exam_flow_inst_a')
        self.assertIsNone(response)
        self.assertEqual(db, 'default')

    def test_institute_without_subdomain_uses_root_and_own_db(self):
        self.c.db_name = 'exam_flow_inst_c'
        self.c.save()
        _, response, db = self.run_middleware(None, self.carol)
        self.assertIsNone(response)
        self.assertEqual(db, 'exam_flow_inst_c')


@with_fake_tenant_dbs
class PortalLoginTests(PortalFixtureMixin, TestCase):
    def test_login_on_own_subdomain(self):
        # 'Alpha' was entered with a capital; hosts are lowercase. This used to 403.
        res = self.login('alice', 'alpha')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn('access', res.json()['tokens'])
        self.assertEqual(res.json()['user']['institute']['subdomain'], 'alpha')

    def test_login_on_other_institutes_subdomain_is_denied(self):
        res = self.login('bob', 'alpha')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()['code'], 'subdomain_access_denied')
        self.assertNotIn('tokens', res.json())

    def test_wrong_password_on_subdomain_does_not_reveal_membership(self):
        res = self.client.post('/api/auth/login/', {'email': 'bob@example.com', 'password': 'wrong'},
                               content_type='application/json', HTTP_HOST=host('alpha'))
        self.assertEqual(res.status_code, 401)

    def test_login_via_membership(self):
        UserInstituteMembership.objects.create(user=self.bob, institute=self.a, role='student')
        self.assertEqual(self.login('bob', 'alpha').status_code, 200)

    def test_inactive_membership_does_not_grant_access(self):
        UserInstituteMembership.objects.create(user=self.bob, institute=self.a, role='student', is_active=False)
        self.assertEqual(self.login('bob', 'alpha').status_code, 403)

    def test_platform_owner_cannot_use_institute_portal(self):
        self.assertEqual(self.login('owner', 'alpha').status_code, 403)
        self.assertEqual(self.login('owner').status_code, 200)

    def test_institute_without_subdomain_logs_in_on_root(self):
        res = self.login('carol')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNone(res.json()['user']['institute']['subdomain'])

    def test_subdomain_institute_can_still_log_in_on_root(self):
        self.assertEqual(self.login('alice').status_code, 200)

    def test_role_login_endpoints_enforce_subdomain(self):
        self.assertEqual(self.login('bob', 'alpha', path='/api/auth/student/login/').status_code, 403)
        self.assertEqual(self.login('alice', 'alpha', path='/api/auth/student/login/').status_code, 200)
        self.assertEqual(self.login('bob', 'alpha', path='/api/timetable/auth/student/login/').status_code, 403)

    def test_stale_token_does_not_block_login(self):
        res = self.client.post(
            '/api/auth/login/', {'email': 'alice@example.com', 'password': 'pass12345'},
            content_type='application/json', HTTP_HOST=host('alpha'), HTTP_AUTHORIZATION=bearer(self.bob),
        )
        self.assertEqual(res.status_code, 200)

    def test_login_selects_tenant_db(self):
        with mock.patch('accounts.tenant_middleware.set_current_db') as set_db:
            self.login('alice')  # root portal -> user's own institute DB
        self.assertEqual(set_db.call_args_list[-1].args[0], 'exam_flow_inst_a')


@with_fake_tenant_dbs
class CrossInstituteIsolationTests(PortalFixtureMixin, TestCase):
    def get(self, path, user, label=None, **headers):
        return self.client.get(path, HTTP_HOST=host(label), HTTP_AUTHORIZATION=bearer(user), **headers)

    def test_token_from_other_institute_rejected_on_subdomain(self):
        res = self.get('/api/auth/profile/', self.bob, 'alpha')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()['code'], 'subdomain_access_denied')

    def test_member_token_accepted_on_subdomain(self):
        res = self.get('/api/auth/profile/', self.alice, 'alpha')
        self.assertEqual(res.status_code, 200, res.content)

    def test_super_admin_cannot_switch_to_other_institute_db(self):
        res = self.get('/api/auth/profile/', self.bella, HTTP_X_INSTITUTE_DB='exam_flow_inst_a')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()['code'], 'institute_access_denied')

    def test_member_may_switch_db_with_header(self):
        UserInstituteMembership.objects.create(user=self.bob, institute=self.a, role='teacher')
        request = RequestFactory().get('/api/exams/', HTTP_HOST=ROOT, HTTP_X_INSTITUTE_DB='exam_flow_inst_a',
                                       HTTP_AUTHORIZATION=bearer(self.bob))
        self.assertIsNone(TenantMiddleware(lambda r: None).process_request(request))
        self.assertEqual(get_current_db(), 'exam_flow_inst_a')

    def test_tenant_user_cannot_read_shared_default_db(self):
        res = self.get('/api/auth/profile/', self.alice, HTTP_X_INSTITUTE_DB='default')
        self.assertEqual(res.status_code, 403)

    def test_super_admin_cannot_list_other_institutes_users(self):
        res = self.get(f'/api/auth/institutes/{self.a.id}/users/', self.bella)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        rows = data['results'] if isinstance(data, dict) else data
        self.assertEqual(rows, [])
        own = self.get(f'/api/auth/institutes/{self.b.id}/users/', self.bella).json()
        own_rows = own['results'] if isinstance(own, dict) else own
        self.assertEqual({u['email'] for u in own_rows}, {'bob@example.com', 'bella@example.com'})

    def test_admin_cannot_invite_into_other_institute(self):
        res = self.client.post(
            '/api/auth/invitations/', {'institute': self.a.id, 'email': 'bella@example.com', 'role': 'super_admin'},
            content_type='application/json', HTTP_HOST=ROOT, HTTP_AUTHORIZATION=bearer(self.bella),
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(InstituteInvitation.objects.get().institute, self.b)

    def test_invitations_scoped_to_own_institute(self):
        InstituteInvitation.objects.create(institute=self.a, email='x@example.com', role='teacher',
                                           invited_by=self.adam, expires_at=timezone.now() + timedelta(days=1))
        data = self.get('/api/auth/invitations/', self.bella).json()
        rows = data['results'] if isinstance(data, dict) else data
        self.assertEqual(rows, [])

    def test_tenant_admin_cannot_change_subdomain(self):
        res = self.client.patch(
            f'/api/auth/institutes/{self.b.id}/update/', {'subdomain': 'alpha2'},
            content_type='application/json', HTTP_HOST=ROOT, HTTP_AUTHORIZATION=bearer(self.bella),
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.b.refresh_from_db()
        self.assertEqual(self.b.subdomain, 'bravo')


@with_fake_tenant_dbs
class PlatformSubdomainApiTests(PortalFixtureMixin, TestCase):
    def patch(self, institute, subdomain):
        return self.client.patch(
            f'/api/auth/platform/institutes/{institute.id}/', {'subdomain': subdomain},
            content_type='application/json', HTTP_HOST=ROOT, HTTP_AUTHORIZATION=bearer(self.owner),
        )

    def test_update_normalizes(self):
        self.assertEqual(self.patch(self.c, '  Gamma-School ').status_code, 200)
        self.c.refresh_from_db()
        self.assertEqual(self.c.subdomain, 'gamma-school')

    def test_clearing_subdomain_stores_null_for_many_institutes(self):
        for inst in (self.a, self.b):
            res = self.patch(inst, '')
            self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.patch(self.c, None).status_code, 200)
        self.assertEqual(Institute.objects.filter(subdomain__isnull=True, is_active=True).count(), 3)

    def test_rejects_invalid_reserved_and_taken(self):
        for value in ['www', 'a', 'bad_name', 'BRAVO', '-x-']:
            with self.subTest(value=value):
                res = self.patch(self.c, value)
                self.assertEqual(res.status_code, 400)
                self.assertIn('subdomain', res.json().get('errors', res.json()))

    def test_two_letter_subdomain(self):
        res = self.patch(self.c, 'HD')
        self.assertEqual(res.status_code, 200, res.content)
        self.c.refresh_from_db()
        self.assertEqual(self.c.subdomain, 'hd')

    def test_field_errors_under_errors_subdomain(self):
        res = self.patch(self.c, 'bravo')
        self.assertEqual(res.status_code, 400)
        self.assertIn('subdomain', res.json()['errors'])

    def test_keeping_own_subdomain_is_not_a_conflict(self):
        self.assertEqual(self.patch(self.b, 'BRAVO').status_code, 200)

    def test_platform_owner_access(self):
        owner = self.make_user('mona', None, role='platform_owner')
        res = self.client.patch(
            f'/api/auth/platform/institutes/{self.c.id}/', {'subdomain': 'hd'},
            content_type='application/json', HTTP_HOST=ROOT, HTTP_AUTHORIZATION=bearer(owner),
        )
        self.assertEqual(res.status_code, 200, res.content)
        res = self.client.get('/api/auth/platform/institutes/', HTTP_HOST=ROOT, HTTP_AUTHORIZATION=bearer(owner))
        self.assertEqual(res.status_code, 200)

    def test_non_owner_cannot_use_platform_endpoint(self):
        res = self.client.patch(
            f'/api/auth/platform/institutes/{self.b.id}/', {'subdomain': 'hijack'},
            content_type='application/json', HTTP_HOST=ROOT, HTTP_AUTHORIZATION=bearer(self.bella),
        )
        self.assertEqual(res.status_code, 403)

    def create(self, **overrides):
        payload = {
            'name': 'Delta', 'contact_email': 'd@example.com', 'admin_email': 'dadmin@example.com',
            'admin_username': 'dadmin', 'admin_first_name': 'D', 'admin_last_name': 'Admin',
            'admin_password': 'pass12345',
        }
        payload.update(overrides)
        return self.client.post('/api/auth/platform/institutes/create-with-admin/', payload,
                                content_type='application/json', HTTP_HOST=ROOT,
                                HTTP_AUTHORIZATION=bearer(self.owner))

    def test_create_without_subdomain(self):
        res = self.create(subdomain='')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIsNone(Institute.objects.get(name='Delta').subdomain)

    def test_create_with_invalid_subdomain(self):
        self.assertEqual(self.create(subdomain='api').status_code, 400)
        self.assertEqual(self.create(subdomain='Alpha').status_code, 400)  # taken, case-insensitive
        self.assertFalse(Institute.objects.filter(name='Delta').exists())

    def test_create_with_valid_subdomain(self):
        self.assertEqual(self.create(subdomain='Delta').status_code, 201)
        self.assertEqual(Institute.objects.get(name='Delta').subdomain, 'delta')


@with_fake_tenant_dbs
class PublicExamAccountTakeoverTests(PortalFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        from exams.models import Exam
        from patterns.models import ExamPattern
        pattern = ExamPattern.objects.create(name='P', institute=self.b, created_by=self.bella,
                                             total_questions=1, total_duration=60, total_marks=4)
        now = timezone.now()
        self.exam = Exam.objects.create(
            title='Open test', institute=self.b, pattern=pattern, created_by=self.bella,
            start_date=now - timedelta(hours=1), end_date=now + timedelta(hours=1),
            duration_minutes=60, status='published', is_public=True,
        )

    def access(self, email):
        return self.client.post('/api/exams/public-access/', {
            'token': str(self.exam.public_access_token), 'first_name': 'X', 'last_name': 'Y', 'email': email,
        }, content_type='application/json', HTTP_HOST=ROOT)

    def test_registered_account_email_gets_no_tokens(self):
        res = self.access('adam@example.com')  # super admin of another institute
        self.assertEqual(res.status_code, 409, res.content)
        self.assertNotIn('access_token', res.json())
        self.adam.refresh_from_db()
        self.assertEqual(self.adam.institute, self.a)

    def test_new_guest_gets_access(self):
        res = self.access('guest@example.com')
        self.assertEqual(res.status_code, 200, res.content)
        guest = User.objects.get(email='guest@example.com')
        self.assertFalse(guest.has_usable_password())
        self.assertEqual(guest.institute, self.b)
        self.assertEqual(self.access('guest@example.com').status_code, 200)  # returning guest

    def test_passwordless_student_account_is_not_treated_as_guest(self):
        student = User.objects.create_user(username='sam', email='sam@example.com', role='student',
                                           institute=self.a)  # no password, but a real account
        self.assertFalse(student.has_usable_password())
        self.assertEqual(self.access('sam@example.com').status_code, 409)
        student.refresh_from_db()
        self.assertEqual(student.institute, self.a)


@with_fake_tenant_dbs
class InstituteEndpointTests(PortalFixtureMixin, TestCase):
    def get(self, path, user=None):
        extra = {'HTTP_AUTHORIZATION': bearer(user)} if user else {}
        return self.client.get(path, HTTP_HOST=ROOT, **extra)

    @staticmethod
    def rows(res):
        data = res.json()
        return data['results'] if isinstance(data, dict) and 'results' in data else data

    def test_anonymous_cannot_list_or_read_institutes(self):
        self.assertEqual(self.get('/api/auth/institutes/').status_code, 401)
        self.assertEqual(self.get(f'/api/auth/institutes/{self.a.id}/').status_code, 401)

    def test_users_see_only_their_institutes(self):
        self.assertEqual({i['name'] for i in self.rows(self.get('/api/auth/institutes/', self.bella))}, {'Beta'})
        UserInstituteMembership.objects.create(user=self.bella, institute=self.c, role='teacher')
        self.assertEqual({i['name'] for i in self.rows(self.get('/api/auth/institutes/', self.bella))}, {'Beta', 'Gamma'})
        self.assertEqual(self.get(f'/api/auth/institutes/{self.a.id}/', self.bella).status_code, 404)
        self.assertEqual(self.get(f'/api/auth/institutes/{self.b.id}/', self.bella).status_code, 200)

    def test_platform_user_sees_all_institutes(self):
        self.assertEqual(len(self.rows(self.get('/api/auth/institutes/', self.owner))), 4)


@with_fake_tenant_dbs
class RoleEscalationTests(PortalFixtureMixin, TestCase):
    def send(self, method, path, user, data):
        return getattr(self.client, method)(path, data, content_type='application/json',
                                            HTTP_HOST=ROOT, HTTP_AUTHORIZATION=bearer(user))

    def test_user_cannot_change_own_role_via_profile(self):
        for role in ['super_admin', 'platform_owner', 'manager']:
            with self.subTest(role=role):
                res = self.send('patch', '/api/auth/profile/', self.alice, {'role': role})
                self.assertEqual(res.status_code, 400, res.content)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.role, 'student')

    def test_profile_edits_without_role_change_still_work(self):
        res = self.send('patch', '/api/auth/profile/', self.alice, {'first_name': 'Alicia', 'role': 'student'})
        self.assertEqual(res.status_code, 200, res.content)

    def test_user_cannot_reactivate_or_verify_self(self):
        res = self.send('patch', '/api/auth/profile/', self.alice, {'is_verified': True})
        self.assertEqual(res.status_code, 400)

    def test_super_admin_can_promote_within_institute_but_not_to_platform(self):
        url = f'/api/auth/users/{self.bob.id}/'
        self.assertEqual(self.send('patch', url, self.bella, {'role': 'teacher'}).status_code, 200)
        self.assertEqual(self.send('patch', url, self.bella, {'role': 'platform_owner'}).status_code, 400)

    def test_lower_admin_cannot_demote_super_admin(self):
        admin = self.make_user('ian', self.b, role='institute_admin')
        res = self.send('patch', f'/api/auth/users/{self.bella.id}/', admin, {'role': 'student'})
        self.assertEqual(res.status_code, 400)

    def test_super_admin_cannot_edit_other_institutes_users(self):
        res = self.send('patch', f'/api/auth/users/{self.alice.id}/', self.bella, {'first_name': 'X'})
        self.assertEqual(res.status_code, 404)

    def test_super_admin_creates_users_only_in_own_institute(self):
        res = self.send('post', '/api/auth/users/', self.bella, {
            'email': 'plant@example.com', 'username': 'plant', 'first_name': 'P', 'last_name': 'Q',
            'password': 'pass12345', 'password_confirm': 'pass12345',
            'institute_id': self.a.id, 'role': 'super_admin',
        })
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(User.objects.get(email='plant@example.com').institute, self.b)

    def test_platform_owner_can_grant_platform_roles(self):
        owner = self.make_user('mona', None, role='platform_owner')
        res = self.send('patch', f'/api/auth/users/{self.carol.id}/', owner, {'role': 'platform_owner'})
        self.assertEqual(res.status_code, 200, res.content)

    def test_cannot_create_platform_owner(self):
        res = self.send('post', '/api/auth/users/', self.bella, {
            'email': 'po@example.com', 'username': 'po', 'first_name': 'P', 'last_name': 'O',
            'password': 'pass12345', 'password_confirm': 'pass12345', 'role': 'platform_owner',
        })
        self.assertEqual(res.status_code, 403)
        self.assertFalse(User.objects.filter(email='po@example.com').exists())

    def test_cannot_move_self_to_other_institutes_center(self):
        from accounts.models import Center
        center_a = Center.objects.create(name='A centre', city='X', institute=self.a)
        res = self.send('patch', '/api/auth/profile/', self.bob, {'center_id': str(center_a.id)})
        self.assertEqual(res.status_code, 400)

    def test_super_admin_cannot_reset_other_institutes_password(self):
        res = self.send('post', f'/api/auth/users/{self.alice.id}/reset-password/', self.bella,
                        {'password': 'newpass123', 'confirm_password': 'newpass123'})
        self.assertEqual(res.status_code, 403)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password('pass12345'))


@with_fake_tenant_dbs
class ExamIsolationTests(PortalFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        from exams.models import Exam, ExamAttempt
        from patterns.models import ExamPattern
        pattern = ExamPattern.objects.create(name='P', institute=self.a, created_by=self.adam,
                                             total_questions=1, total_duration=60, total_marks=4)
        now = timezone.now()
        self.exam = Exam.objects.create(
            title='A exam', institute=self.a, pattern=pattern, created_by=self.adam,
            start_date=now - timedelta(hours=1), end_date=now + timedelta(hours=1),
            duration_minutes=60, status='published',
        )
        self.attempt = ExamAttempt.objects.create(exam=self.exam, student=self.alice)

    def get(self, path, user):
        return self.client.get(path, HTTP_HOST=ROOT, HTTP_AUTHORIZATION=bearer(user))

    def test_super_admin_cannot_open_other_institutes_results(self):
        url = f'/api/exams/exams/{self.exam.id}/results-dashboard/'
        self.assertEqual(self.get(url, self.bella).status_code, 403)

    def test_center_admin_cannot_open_other_institutes_snapshots(self):
        center_admin = self.make_user('cid', self.b, role='admin')
        url = f'/api/exams/attempts/{self.attempt.id}/proctoring/snapshots/'
        self.assertEqual(self.get(url, center_admin).status_code, 403)

    def test_super_admin_cannot_read_other_institutes_answer_key(self):
        res = self.get(f'/api/omr/answer-keys/exam/{self.exam.id}/', self.bella)
        self.assertEqual(res.status_code, 404)


class SharedMembershipTests(SimpleTestCase):
    def test_memberships_always_route_to_shared_db(self):
        from accounts.router import InstituteRouter
        from accounts.utils import set_current_db
        set_current_db('exam_flow_inst_a')
        try:
            router = InstituteRouter()
            self.assertEqual(router.db_for_read(UserInstituteMembership), 'default')
            self.assertEqual(router.db_for_write(UserInstituteMembership), 'default')
        finally:
            clear_current_db()


class AllowedHostsTests(TestCase):
    def test_unknown_host_is_rejected(self):
        self.assertEqual(self.client.get('/api/auth/institutes/', HTTP_HOST='evil.example.com').status_code, 400)

    def test_portal_hosts_are_allowed(self):
        res = self.client.get('/api/auth/institutes/by-subdomain/nope/', HTTP_HOST=f'www.{ROOT}')
        self.assertEqual(res.status_code, 404)  # routed, not a DisallowedHost 400
