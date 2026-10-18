"""Validate test design integrity only. This is not an Agent execution or PASS verdict."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

CHECK_KEYS = {
    'operations_any', 'operations_all', 'operations_forbidden', 'reads_any', 'clinic_details',
    'service_details', 'package_details', 'patient_details', 'safety_signals', 'handoff_category',
    'handoff_priority', 'response_disposition', 'package_sessions', 'semantic_date', 'semantic_time',
    'time_mode', 'task_is_none', 'task_type', 'task_service_ids', 'device', 'doctor', 'doctor_is_none',
    'date', 'price_minor_contains', 'price_minor_contains_all', 'no_authorized_transaction',
}


def validate():
    suite = json.loads(Path(__file__).with_name('customer_scenarios.json').read_text())
    ids, signatures = set(), set()
    for case in suite['cases']:
        assert case['id'] not in ids, f"Duplicate ID: {case['id']}"
        ids.add(case['id'])
        assert case['turns'] and case['source'] and case['invariant'] and case['preconditions']
        assert case['status'] == 'NOT_RUN', 'Test design must not manufacture execution results'
        assert case['execution_level'] in {'LIVE_FIXTURE', 'INTEGRATION'}
        assert datetime.fromisoformat(case['clock']).tzinfo is not None
        assert all(1 <= turn <= len(case['turns']) for turn in case['allowed_transactional_write_turns'])
        for turn in case['turns']:
            assert turn['customer'] and turn['expected']
            assert set(turn['checks']) <= CHECK_KEYS, f"Unsupported check: {case['id']}"
        signature = (case['fixture'], case['clock'], tuple(t['customer'] for t in case['turns']))
        assert signature not in signatures, f"Duplicate conversation and setup: {case['id']}"
        signatures.add(signature)
        if case['execution_level'] == 'LIVE_FIXTURE':
            assert case['fixture'] == 'default', f"Missing fixture adapter: {case['id']}"
            assert not case['allowed_transactional_write_turns'], 'Fixture runner cannot prove persisted writes'
    result = {'design_validation': 'VALID', 'product_execution': 'NOT_RUN', 'case_count': len(ids),
              'turn_count': sum(len(c['turns']) for c in suite['cases']),
              'category_count': len(suite['categories']),
              'execution_levels': dict(Counter(c['execution_level'] for c in suite['cases']))}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    validate()
