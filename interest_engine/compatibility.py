"""Metric-specific compatibility; preserve recorded source versions in artifacts."""


def interest_definition(definition: dict) -> dict:
    result = dict(definition)
    # 15.0.0 changes safety matching only; interest inputs and classifications
    # are reconciled against 14.0.0 before the operational release.
    if (result.get('engineVersion'), result.get('detailSchema'),
        result.get('baseCoreVersion'), result.get('baseRulesVersion')) == (
            'teeni-interest-engine/1.5.0', 'teeni-interest-detail/1.2.0', '2.6.1', '15.0.0'):
        result.update(baseCoreVersion='2.6.0', baseRulesVersion='14.0.0')
    return result
