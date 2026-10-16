"""
Clean existing Institute.subdomain values before 0020 tightens the column:

  - trim + lowercase ('KIITIS ' -> 'kiitis'), which is what hosts look like;
    mixed-case values previously never matched the case-sensitive lookup
  - '' -> NULL, so any number of institutes can have no subdomain
  - values that are invalid, reserved, or collide after lowercasing -> NULL
    (printed so they can be reassigned by hand)
"""
from django.db import migrations

from accounts.subdomains import normalize_subdomain, subdomain_error


def cleaned_subdomains(rows):
    """
    rows: (pk, subdomain) ordered by pk. Returns [(pk, new_value, reason)]
    for each row whose stored value must change; reason is set when a value
    is cleared rather than just normalised. The lowest pk keeps a duplicate.
    Clears come first so a rename never collides with a not-yet-cleared row.
    """
    changes = []
    seen = set()
    for pk, original in rows:
        value = normalize_subdomain(original)
        reason = None
        if value is not None:
            reason = subdomain_error(value) or ('duplicate' if value in seen else None)
            if reason:
                value = None
            else:
                seen.add(value)
        if value != original:
            changes.append((pk, value, reason))
    return sorted(changes, key=lambda change: change[1] is not None)


def normalize_subdomains(apps, schema_editor):
    db = schema_editor.connection.alias
    if db != 'default':  # Institute lives only in the shared DB
        return
    Institute = apps.get_model('accounts', 'Institute')
    rows = list(Institute.objects.using(db).exclude(subdomain__isnull=True).order_by('id').values_list('id', 'subdomain'))
    originals = dict(rows)
    for pk, value, reason in cleaned_subdomains(rows):
        if reason:
            print(f"\n  [subdomain] institute {pk}: cleared {originals[pk]!r} ({reason}); reassign it by hand")
        Institute.objects.using(db).filter(pk=pk).update(subdomain=value)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0018_add_subdomain_to_institute"),
    ]

    operations = [
        migrations.RunPython(normalize_subdomains, migrations.RunPython.noop),
    ]
