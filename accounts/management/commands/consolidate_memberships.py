"""
UserInstituteMembership now lives only in the shared ('default') database.
Rows written to a tenant database before that change are no longer read.
This copies them into the shared DB (dry run unless --apply).
"""
from django.core.management.base import BaseCommand
from django.db import connections
from django.db.utils import OperationalError

from accounts.database_utils import register_institute_database
from accounts.models import Institute, UserInstituteMembership

TABLE = UserInstituteMembership._meta.db_table
COLUMNS = ('user_id', 'institute_id', 'role', 'teacher_code', 'center_id', 'is_active')


class Command(BaseCommand):
    help = 'Copy institute memberships stored in tenant databases into the shared database.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the missing rows (default: report only)')

    def handle(self, *args, **options):
        apply = options['apply']
        missing_total = 0
        institutes = Institute.objects.using('default').exclude(db_name__isnull=True).exclude(db_name='')
        for institute in institutes:
            db = register_institute_database(institute)
            try:
                connection = connections[db]
                if TABLE not in connection.introspection.table_names():
                    continue
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT {", ".join(COLUMNS)} FROM {TABLE}')
                    rows = [dict(zip(COLUMNS, row)) for row in cursor.fetchall()]
            except OperationalError as e:
                self.stderr.write(self.style.WARNING(f"Skipping {db}: {str(e).strip().splitlines()[-1]}"))
                continue

            existing = set(UserInstituteMembership.objects.using('default').values_list('user_id', 'institute_id'))
            missing = [r for r in rows if (r['user_id'], r['institute_id']) not in existing]
            missing_total += len(missing)
            self.stdout.write(f"{db}: {len(rows)} membership rows, {len(missing)} not in the shared DB")
            if apply:
                for row in missing:
                    UserInstituteMembership.objects.using('default').create(**row)

        verb = 'Copied' if apply else 'Would copy'
        self.stdout.write(self.style.SUCCESS(f"{verb} {missing_total} membership(s)."))
        if missing_total and not apply:
            self.stdout.write("Re-run with --apply to copy them.")
