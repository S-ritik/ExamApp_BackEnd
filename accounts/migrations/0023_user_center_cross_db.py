from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """
    accounts_user (shared DB) -> accounts_center (tenant DB) cannot be a real
    FK: for institutes with their own DB the center row is not in the shared
    DB, so assigning a center raised IntegrityError on user save. Same fix as
    0021 for UserInstituteMembership.center.
    """

    dependencies = [
        ('accounts', '0022_remove_manager_role'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='center',
            field=models.ForeignKey(blank=True, db_constraint=False, help_text='Center to which the user belongs (timetable system).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='users', to='accounts.center'),
        ),
    ]
