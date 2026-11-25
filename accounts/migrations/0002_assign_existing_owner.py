"""Multi-user: everything made before users existed belongs to the first admin (the operator)."""

from django.db import migrations


def assign(apps, schema_editor):
    User = apps.get_model("auth", "User")
    op = User.objects.filter(is_superuser=True, is_active=True).order_by("id").first() or User.objects.order_by("id").first()
    if op is None:
        return
    for app, model in (("trading", "StrategyConfig"), ("cascades", "Cascade"), ("cascades", "CascadeBacktest"),
                       ("ledger", "Account")):
        apps.get_model(app, model).objects.filter(owner__isnull=True).update(owner=op)
    TC = apps.get_model("trading", "TradingControl")
    if not TC.objects.filter(owner=op).exists():
        TC.objects.filter(pk=1, owner__isnull=True).update(owner=op)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("trading", "0011_strategyconfig_owner_tradingcontrol_owner_and_more"),
        ("cascades", "0002_cascade_owner_cascadebacktest_owner"),
        ("ledger", "0002_account_owner_alter_account_name_and_more"),
    ]

    operations = [migrations.RunPython(assign, migrations.RunPython.noop)]
