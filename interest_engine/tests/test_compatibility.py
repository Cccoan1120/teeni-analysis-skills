import unittest

from interest_engine.compatibility import interest_definition
from interest_engine.history import same_history_contract


class SafetyRevisionCompatibilityTests(unittest.TestCase):
    def definition(self, core='2.6.0', rules='14.0.0'):
        return {'engineVersion': 'teeni-interest-engine/1.5.0', 'detailSchema': 'teeni-interest-detail/1.2.0',
                'baseCoreVersion': core, 'baseRulesVersion': rules}

    def test_only_reviewed_pair_bridges_and_source_is_preserved(self):
        new = self.definition('2.6.1', '15.0.0')
        self.assertEqual(interest_definition(new), self.definition())
        self.assertEqual(new['baseRulesVersion'], '15.0.0')
        for core, rules in [('2.6.1', '14.0.0'), ('2.6.0', '15.0.0'), ('2.6.2', '15.0.0')]:
            value = self.definition(core, rules)
            self.assertEqual(interest_definition(value), value)
        for key in ('engineVersion', 'detailSchema'):
            value = {**new, key: 'unreviewed'}
            self.assertEqual(interest_definition(value), value)

    def test_history_preserves_other_boundaries(self):
        def snapshot(core, rules):
            definition = self.definition(core, rules)
            return {'productVersion': 'M1', 'sceneId': '488', 'registrySha256': 'registry',
                    'engineVersion': definition['engineVersion'], 'detailSchema': definition['detailSchema'],
                    'schemaVersion': 'teeni-interest-snapshot/1.5.0',
                    'method': {'baseContract': 'teeni-base-detail/1.2.0', 'baseCoreVersion': core, 'baseRulesVersion': rules}}
        old, new = snapshot('2.6.0', '14.0.0'), snapshot('2.6.1', '15.0.0')
        self.assertTrue(same_history_contract(old, new))
        self.assertFalse(same_history_contract(old, {**new, 'registrySha256': 'different'}))
        self.assertFalse(same_history_contract(old, {**new, 'productVersion': 'M2', 'sceneId': '904'}))
