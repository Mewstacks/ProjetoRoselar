from django.db import migrations


LEGACY_COLUMN = "selected_cash_payment_type"


def allow_inserts_with_legacy_column(apps, schema_editor):
    """Keep installs with the retired column writable without deleting its data.

    An older production schema still contains this NOT NULL column although it
    is no longer part of ``Quote``. Giving it an empty-string database default
    lets the current ORM insert quotes while preserving every historical value.
    """
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, "sales_quote"
            )
        }
    if LEGACY_COLUMN not in columns:
        return
    if connection.vendor == "postgresql":
        table = schema_editor.quote_name("sales_quote")
        column = schema_editor.quote_name(LEGACY_COLUMN)
        schema_editor.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT ''"
        )


class Migration(migrations.Migration):
    dependencies = [("sales", "0038_quote_freight_options")]

    operations = [
        migrations.RunPython(
            allow_inserts_with_legacy_column,
            reverse_code=migrations.RunPython.noop,
        )
    ]
