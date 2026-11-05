"""Small synthetic checks for the compact-index retention gate."""
import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    'check_rule_index', Path(__file__).parents[1] / 'tools/check_rule_index.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

INDEX = ('## About the operator\n\nKeep answers brief.\n\n'
         '## Instance rules\n\n- R01 Before publishing.\n- R02 Before deleting.\n')
RULES = ('<!-- rule:R01 -->\n- **Publish:** ask first.\n\n'
         '<!-- rule:R02 -->\n- **Delete:** preserve real data.\n')


def test_complete_index_retains_original_requirements():
    baseline = INDEX.split('## Instance rules')[0] + '## Instance rules\n\n- **Publish:** ask first.\n'
    assert module.check(INDEX, RULES, baseline_rules=RULES,
                        baseline_index=baseline)['errors'] == []


def test_missing_duplicate_or_unbacked_index_entry_refuses():
    for changed in (INDEX.replace('- R02 Before deleting.\n', ''),
                    INDEX + '- R01 Duplicate.\n', INDEX + '- R03 Unknown.\n'):
        assert module.check(changed, RULES)['errors']


def test_rule_rewrite_and_missing_original_summary_refuse():
    changed = RULES.replace('ask first', 'proceed freely')
    assert module.check(INDEX, changed, baseline_rules=RULES)['errors']
    baseline = INDEX.split('## Instance rules')[0] + '## Instance rules\n\n- **Missing:** keep this.\n'
    assert module.check(INDEX, RULES, baseline_index=baseline)['errors']


def test_budget_operator_change_and_empty_rule_refuse():
    assert module.check(INDEX, RULES, budget=10)['errors']
    assert module.check(INDEX.replace('brief', 'long'), RULES,
                        baseline_index=INDEX)['errors']
    assert module.check(INDEX, RULES.replace('- **Delete:** preserve real data.', ''))['errors']


def test_original_summary_must_be_attached_to_its_mapped_rule():
    line = '- **Publish:** ask first.'
    baseline = INDEX.split('## Instance rules')[0] + '## Instance rules\n\n' + line + '\n'
    mapping = {line: ['R01']}
    assert module.check(INDEX, RULES, baseline_index=baseline,
                        summary_map=mapping)['errors'] == []
    misplaced = RULES.replace(line, '- **Publish:** historical wording.') + '\n  ' + line + '\n'
    assert module.check(INDEX, misplaced, baseline_index=baseline,
                        summary_map=mapping)['errors']
    assert module.check(INDEX, RULES, baseline_index=baseline,
                        summary_map={})['errors']
