"""Reported-return scenario arithmetic, never executable financial evidence."""
from __future__ import annotations

import math
from typing import Any, Iterable


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int,float,str)):
        return None
    try:
        number=float(value)
    except (ValueError,TypeError,OverflowError):
        return None
    return number if math.isfinite(number) else None


def reported_value(row: dict, keys: Iterable[str]) -> float | None:
    # Canonical zero is a value. An invalid supplied canonical value cannot be
    # replaced by a convenient alias; genuinely absent fields may fall back.
    for key in keys:
        if row.get(key) is not None:
            return finite_number(row[key])
    return None


def linear_sum(terms: Iterable[tuple[float,float]]) -> float | None:
    terms=list(terms)
    if not terms:
        return 0.
    scale=max(abs(value) for value,_ in terms)
    if not scale:
        return 0.
    result=scale*math.fsum((value/scale)*weight for value,weight in terms)
    return result if math.isfinite(result) else None


def rounded(value: float | None, digits: int=3) -> float | None:
    return round(value,digits) if value is not None else None


def moments(values: list[float]) -> dict:
    if not values:
        return {'total_pnl':None,'avg_pnl':None,'median_pnl':None}
    ordered=sorted(values)
    middle=len(values)//2
    median=(ordered[middle] if len(values)%2 else
            linear_sum([(ordered[middle-1],.5),(ordered[middle],.5)]))
    return {'total_pnl':rounded(linear_sum((value,1.) for value in values)),
        'avg_pnl':rounded(linear_sum((value,1./len(values)) for value in values)),
        'median_pnl':rounded(median)}


def replay_scope(*, input_rows: int | None=None) -> dict:
    # Fixed by this method's contract, never copied from caller-supplied flags.
    return {'schema_version':1,'diagnostic_only':True,'financial_acceptance':False,
        'can_authorize_trading':False,'prospective':False,'execution_costs_verified':False,
        'population_grain':'historical_closed_candidate_observations_and_position_snapshots',
        'method':'posthoc_heuristic_policy_scenarios_not_executable_counterfactual_fills',
        'metric_units':{'trades':'legacy_name_for_reported_outcome_observations',
            'total_pnl':'unweighted_reported_percentage_points_sum',
            'avg_pnl':'reported_percent_per_valid_outcome'},
        'input_rows':input_rows,
        'limitations':['Mixed historical sources and hypothetical exits are not a prospective net-cash strategy cohort.',
            'Reported percent-point sums are not USD, SOL or portfolio return.',
            'Source de-duplication, fill/cost reconciliation and causal opportunity coverage are not certified by this replay.']}


def diagnostic_metrics(value: Any) -> dict | None:
    if not isinstance(value,dict) or type(value.get('trades')) is not int or value['trades']<=0:
        return None
    for field in ('total_pnl','runner_capture_ratio'):
        if type(value.get(field)) not in (int,float) or finite_number(value[field]) is None:
            return None
    losses=value.get('severe_loss_count')
    if type(losses) is not int or not 0<=losses<=value['trades'] or value['runner_capture_ratio']<0:
        return None
    return value


def diagnostic_comparison(current: Any, candidate: Any) -> dict:
    baseline,challenger=diagnostic_metrics(current),diagnostic_metrics(candidate)
    comparable=bool(baseline is not None and challenger is not None
        and baseline['trades']==challenger['trades'])
    passed=bool(comparable and challenger['total_pnl']>=baseline['total_pnl']
        and challenger['severe_loss_count']<=baseline['severe_loss_count']
        and challenger['runner_capture_ratio']>=baseline['runner_capture_ratio'])
    return {'passed':passed,'comparable':comparable,'financial_acceptance':False,
        'basis':'same_count_finite_reported_scenario_metrics_not_verified_paired_financial_cohort'}
