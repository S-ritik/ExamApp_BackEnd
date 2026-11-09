"""
POST /api/auth/register/ — the endpoint the admin People/Users pages use to
create students.

    python manage.py test accounts.tests_registration --settings=exam_flow_backend.test_settings
"""
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import Center, Institute, User
from accounts.utils import clear_current_db

URL = '/api/auth/register/'
PASSWORD = 'Student@2026'


def bearer(user):
    return f'Bearer {RefreshToken.for_user(user).access_token}'


@override_settings(ALLOWED_HOSTS=['*'])
@mock.patch('accounts.utils.send_credentials_email')
class StudentRegistrationTests(TestCase):
    def setUp(self):
        clear_current_db()
        self.institute = Institute.objects.create(name='KIITIS')
        self.admin = User.objects.create_user(
            username='admin', email='admin@kiitis.ac.in', password='x',
            role='super_admin', institute=self.institute,
        )
        self.center = Center.objects.create(name='Main', city='Bhubaneswar', institute=self.institute)

    def tearDown(self):
        clear_current_db()

    def payload(self, **overrides):
        # Mirrors studentPayload in UsersContent.tsx / AdminPeopleContent.tsx
        data = {
            'username': 'STU-asha', 'email': 'asha@kiitis.ac.in',
            'password': PASSWORD, 'password_confirm': PASSWORD,
            'first_name': 'Asha', 'last_name': 'Rao',
            'role': 'student', 'institute_id': self.institute.id,
        }
        data.update(overrides)
        return data

    def post(self, data, user=None):
        headers = {'HTTP_AUTHORIZATION': bearer(user)} if user else {}
        return self.client.post(URL, data, content_type='application/json', **headers)

    def test_admin_creates_student(self, _email):
        res = self.post(self.payload(), user=self.admin)
        self.assertEqual(res.status_code, 201, res.content)
        student = User.objects.get(email='asha@kiitis.ac.in')
        self.assertEqual(student.role, 'student')
        self.assertEqual(student.institute_id, self.institute.id)

    def test_admin_creates_student_with_center(self, _email):
        res = self.post(self.payload(center_id=str(self.center.id)), user=self.admin)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(User.objects.get(email='asha@kiitis.ac.in').center_id, self.center.id)

    def test_username_optional(self, _email):
        data = self.payload()
        del data['username']
        res = self.post(data, user=self.admin)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(User.objects.get(email='asha@kiitis.ac.in').username, 'asha')

    def test_uppercase_role_is_normalized(self, _email):
        res = self.post(self.payload(role='STUDENT'), user=self.admin)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(User.objects.get(email='asha@kiitis.ac.in').role, 'student')

    def test_duplicate_email_is_400(self, _email):
        self.post(self.payload(), user=self.admin)
        res = self.post(self.payload(username='other'), user=self.admin)
        self.assertEqual(res.status_code, 400, res.content)

    def test_duplicate_username_is_400(self, _email):
        self.post(self.payload(), user=self.admin)
        res = self.post(self.payload(email='other@kiitis.ac.in'), user=self.admin)
        self.assertEqual(res.status_code, 400, res.content)

    def test_malformed_center_id_is_400(self, _email):
        res = self.post(self.payload(center_id='not-a-uuid'), user=self.admin)
        self.assertEqual(res.status_code, 400, res.content)

    def test_unknown_center_id_is_400(self, _email):
        res = self.post(self.payload(center_id='00000000-0000-0000-0000-000000000000'), user=self.admin)
        self.assertEqual(res.status_code, 400, res.content)

    def test_unknown_role_is_400(self, _email):
        res = self.post(self.payload(role='wizard'), user=self.admin)
        self.assertEqual(res.status_code, 400, res.content)

    def test_anonymous_cannot_self_register_as_platform_owner(self, _email):
        res = self.post(self.payload(role='platform_owner', institute_id=None))
        self.assertEqual(res.status_code, 403, res.content)
        self.assertFalse(User.objects.filter(email='asha@kiitis.ac.in').exists())

    def test_anonymous_self_registration_still_works(self, _email):
        # Register.tsx: public sign-up without an institute (onboarding flow)
        data = self.payload(institute_id=None)
        del data['role']
        res = self.post(data)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.json()['onboarding_required'])
        self.assertEqual(User.objects.get(email='asha@kiitis.ac.in').role, 'student')

    def test_anonymous_cannot_join_an_institute(self, _email):
        res = self.post(self.payload())
        self.assertEqual(res.status_code, 403, res.content)

    def test_admin_cannot_create_in_other_institute(self, _email):
        other = Institute.objects.create(name='Other')
        res = self.post(self.payload(institute_id=other.id), user=self.admin)
        self.assertEqual(res.status_code, 403, res.content)

    def test_center_from_other_institute_is_400(self, _email):
        other = Institute.objects.create(name='Other')
        foreign = Center.objects.create(name='Far', city='Pune', institute=other)
        res = self.post(self.payload(center_id=str(foreign.id)), user=self.admin)
        self.assertEqual(res.status_code, 400, res.content)

    def test_admin_cannot_create_platform_owner(self, _email):
        res = self.post(self.payload(role='platform_owner'), user=self.admin)
        self.assertEqual(res.status_code, 403, res.content)

    def test_usage_metric_failure_does_not_lose_the_student(self, _email):
        with mock.patch('billing.models.UsageMetric.objects.create', side_effect=Exception('boom')):
            res = self.post(self.payload(), user=self.admin)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(User.objects.filter(email='asha@kiitis.ac.in').exists())
