#!/usr/bin/env python3
"""Preview or create a root-compartment monthly budget and actual-spend email alert."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import oci


def validate(settings):
    amount = settings.get('amount', 1)
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 1 or amount != int(amount):
        raise ValueError('amount must be a positive whole number in account billing currency')
    emails = settings.get('recipients', [])
    if not isinstance(emails, list) or not emails or any(not isinstance(e, str) or not re.fullmatch(r'[^\s,@]+@[^\s,@]+\.[^\s,@]+', e) for e in emails):
        raise ValueError('recipients must contain valid email addresses')
    if not settings.get('currency_confirmed') or settings['currency_confirmed'] is not True:
        raise ValueError('verify account billing currency and set currency_confirmed=true')
    if not re.fullmatch(r'[A-Z]{3}', settings.get('billing_currency', '')):
        raise ValueError('billing_currency must be the verified three-letter currency code')
    return amount, sorted(set(emails))


def items(api, method, *args):
    return oci.pagination.list_call_get_all_results(getattr(api, method), *args).data


def reconcile(api, tenancy, settings, apply=False):
    amount, emails = validate(settings)
    name = settings.get('budget_name', 'free-tier-monthly-alert')
    budgets = [b for b in items(api, 'list_budgets', tenancy) if b.lifecycle_state != 'DELETED']
    matches = [b for b in budgets if b.display_name == name]
    if len(matches) > 1:
        raise ValueError('multiple budgets with requested name; inspect in console')
    budget = matches[0] if matches else None
    if budget and not (budget.target_type == 'COMPARTMENT' and budget.targets == [tenancy] and budget.reset_period == 'MONTHLY' and budget.processing_period_type == 'MONTH' and budget.amount == amount and budget.lifecycle_state == 'ACTIVE'):
        raise ValueError('existing named budget differs from requested root monthly budget; no changes made')
    other_roots = [b for b in budgets if b is not budget and b.target_type == 'COMPARTMENT' and tenancy in (b.targets or [])]
    if not budget and other_roots:
        raise ValueError('root budget already exists; inspect it and set budget_name to reuse it, rather than duplicate')
    token = hashlib.sha256(json.dumps([tenancy, name, amount, emails]).encode()).hexdigest()
    plan = {'scope': 'root compartment and children', 'amount': amount,
            'billing_currency': settings['billing_currency'], 'currency_source': 'user-confirmed; API uses account rate-card currency',
            'budget': 'existing' if budget else 'create', 'alert': 'create', 'applied': apply}
    rules = items(api, 'list_alert_rules', budget.id) if budget else []
    for rule in rules:
        recipients = sorted(set(e.strip() for e in (rule.recipients or '').split(',') if e.strip()))
        if rule.type == 'ACTUAL' and rule.threshold_type == 'PERCENTAGE' and rule.threshold == 100 and recipients == emails:
            plan['alert'] = 'existing'
            return plan
    if not apply:
        return plan
    # No retries on uncertain writes. Stable retry tokens and server-side list reconciliation
    # protect repeat invocations; do not run concurrent writers for the same account.
    if budget is None:
        budget = api.create_budget(oci.budget.models.CreateBudgetDetails(
            compartment_id=tenancy, target_type='COMPARTMENT', targets=[tenancy],
            display_name=name, amount=amount, reset_period='MONTHLY', processing_period_type='MONTH',
            description='Monthly actual spending email alert; does not cap spending'), opc_retry_token=token).data
        confirmed = api.get_budget(budget.id).data
        if confirmed.amount != amount or confirmed.targets != [tenancy]:
            raise ValueError('budget readback mismatch; inspect console before retrying')
    response = api.create_alert_rule(budget.id, oci.budget.models.CreateAlertRuleDetails(
        display_name='actual-100-percent', type='ACTUAL', threshold=100,
        threshold_type='PERCENTAGE', recipients=','.join(emails),
        message='Monthly spending threshold reached. Check OCI billing and resource usage.'), opc_retry_token=token)
    rule = api.get_alert_rule(budget.id, response.data.id).data
    if rule.type != 'ACTUAL' or rule.threshold != 100 or rule.threshold_type != 'PERCENTAGE' or sorted(e.strip() for e in rule.recipients.split(',')) != emails:
        raise ValueError('alert readback mismatch; inspect console before retrying')
    plan['verified'] = True
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--oci-config', default='~/.oci/config')
    parser.add_argument('--apply', action='store_true', help='create missing budget/alert; default read-only')
    args = parser.parse_args()
    try:
        settings = json.loads(Path(args.settings).expanduser().read_text())
        validate(settings)
        cfg = oci.config.from_file(str(Path(args.oci_config).expanduser()), settings['profile'])
        # Budget service is managed through the tenancy home region.
        identity = oci.identity.IdentityClient(cfg, timeout=(5, 20), retry_strategy=oci.retry.NoneRetryStrategy())
        regions = identity.list_region_subscriptions(cfg['tenancy']).data
        home = next(r.region_name for r in regions if r.is_home_region)
        cfg['region'] = home
        api = oci.budget.BudgetClient(cfg, timeout=(5, 20), retry_strategy=oci.retry.NoneRetryStrategy())
        print(json.dumps(reconcile(api, cfg['tenancy'], settings, args.apply), ensure_ascii=False, indent=2))
    except Exception as exc:
        # SDK exceptions may contain request metadata. Keep public logs bounded.
        print(f'Budget operation stopped ({type(exc).__name__}). {str(exc) if isinstance(exc, ValueError) else "Inspect OCI console before retrying any uncertain write."}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
