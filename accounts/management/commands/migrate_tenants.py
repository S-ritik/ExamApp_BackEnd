from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.utils import OperationalError

from accounts.database_utils import register_institute_database
from accounts.models import Institute


class Command(BaseCommand):
    help = 'Run migrations on every institute (tenant) database. Run after `migrate` on each deploy.'

    def add_arguments(self, parser):
        parser.add_argument('--plan', action='store_true', help='Show planned migrations without applying them')
        parser.add_argument('--fake', action='store_true', help='Mark migrations as applied without running them')
        parser.add_argument('--institute', type=int, help='Only migrate this institute ID')

    def handle(self, *args, **options):
        institutes = Institute.objects.using('default').exclude(db_name__isnull=True).exclude(db_name='')
        if options['institute']:
            institutes = institutes.filter(id=options['institute'])

        failed = []
        for institute in institutes:
            db = register_institute_database(institute)
            try:
                connections[db].ensure_connection()
            except OperationalError as e:
                self.stderr.write(self.style.WARNING(
                    f"Skipping {institute.name} ({db}): {str(e).strip().splitlines()[-1]}"
                ))
                continue

            self.stdout.write(self.style.MIGRATE_HEADING(f"==> {institute.name} ({db})"))
            try:
                call_command(
                    'migrate',
                    database=db,
                    plan=options['plan'],
                    fake=options['fake'],
                    interactive=False,
                    verbosity=options['verbosity'],
                )
            except Exception as e:  # keep going: one broken tenant must not block the rest
                failed.append(db)
                self.stderr.write(self.style.ERROR(f"Failed on {db}: {e}"))

        if failed:
            raise CommandError(f"Migrations failed for: {', '.join(failed)}")
