from pathlib import Path
import sys
from types import SimpleNamespace as S
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import budget


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(amount=1, billing_currency='USD', currency_confirmed=True,
                             recipients=['test@example.com'])
        self.api = Mock()
        self.existing = S(id='b1', display_name='free-tier-monthly-alert',
                          target_type='COMPARTMENT', targets=['root'], reset_period='MONTHLY',
                          processing_period_type='MONTH', amount=1, lifecycle_state='ACTIVE')
        self.rule = S(id='r1', type='ACTUAL', threshold_type='PERCENTAGE', threshold=100,
                      recipients='test@example.com')
        self.rows = {'list_budgets': [], 'list_alert_rules': []}
        self.patcher = patch.object(budget, 'items', side_effect=lambda api, method, *a: self.rows[method])
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_preview_never_writes(self):
        result = budget.reconcile(self.api, 'root', self.settings)
        self.assertEqual(result['budget'], 'create')
        self.api.create_budget.assert_not_called()
        self.api.create_alert_rule.assert_not_called()

    def test_existing_reused(self):
        self.rows['list_budgets'] = [self.existing]
        self.rows['list_alert_rules'] = [self.rule]
        self.assertEqual(budget.reconcile(self.api, 'root', self.settings, True)['alert'], 'existing')
        self.api.create_budget.assert_not_called()
        self.api.create_alert_rule.assert_not_called()

    def test_conflict_does_not_write(self):
        self.existing.amount = 5
        self.rows['list_budgets'] = [self.existing]
        with self.assertRaises(ValueError):
            budget.reconcile(self.api, 'root', self.settings, True)
        self.api.create_budget.assert_not_called()

    def test_other_root_budget_blocks_duplicate(self):
        self.existing.display_name = 'user-existing'
        self.rows['list_budgets'] = [self.existing]
        with self.assertRaises(ValueError):
            budget.reconcile(self.api, 'root', self.settings, True)

    def test_create_and_readback(self):
        self.api.create_budget.return_value.data = self.existing
        self.api.get_budget.return_value.data = self.existing
        self.api.create_alert_rule.return_value.data = self.rule
        self.api.get_alert_rule.return_value.data = self.rule
        result = budget.reconcile(self.api, 'root', self.settings, True)
        self.assertTrue(result['verified'])
        details = self.api.create_budget.call_args.args[0]
        self.assertEqual(details.targets, ['root'])
        self.assertEqual(details.processing_period_type, 'MONTH')
        self.assertEqual(self.api.create_alert_rule.call_args.args[1].threshold, 100)

    def test_uncertain_write_stops_before_alert(self):
        self.api.create_budget.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            budget.reconcile(self.api, 'root', self.settings, True)
        self.api.create_budget.assert_called_once()
        self.api.create_alert_rule.assert_not_called()

    def test_currency_confirmation_required(self):
        self.settings['currency_confirmed'] = False
        with self.assertRaises(ValueError):
            budget.reconcile(self.api, 'root', self.settings, True)
        self.api.create_budget.assert_not_called()
