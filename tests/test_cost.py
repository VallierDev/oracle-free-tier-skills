from pathlib import Path
import sys
from types import SimpleNamespace as S
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import traffic

class CostTests(unittest.TestCase):
    def test_currency_and_credits(self):
        rows = [S(is_forecast=False, computed_amount=a, currency=c) for a,c in [(2,'USD'),(-.5,'USD'),(3,'SGD')]]
        self.assertEqual(traffic.summarize_cost(rows), {'SGD':'3','USD':'1.5'})

    def test_empty_and_invalid_are_unknown(self):
        for rows in [[], [S(is_forecast=True, computed_amount=1,currency='USD')], [S(is_forecast=False,computed_amount=None,currency='USD')], [S(is_forecast=False,computed_amount=float('nan'),currency='USD')]]:
            with self.assertRaises(ValueError):
                traffic.summarize_cost(rows)

    def test_recorded_zero_is_valid(self):
        self.assertEqual(traffic.summarize_cost([S(is_forecast=False,computed_amount=0,currency='USD')]), {'USD':'0'})
