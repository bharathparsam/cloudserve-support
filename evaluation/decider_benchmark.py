"""Development-calibration-only retrieval benchmark; fixture replay is never live evidence."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from src.config import Settings
from src.ingest import digest, normalize, now


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def trusted_calibration(development, settings, limit=None):
    """Reject unknown bytes before JSON parsing, then select only frozen calibration indexes."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError('Limit must be a positive integer')
    manifest = json.loads((settings.classifier_dir / 'manifest.json').read_text())
    split_bytes = (settings.classifier_dir / 'split-manifest.json').read_bytes()
    if hashlib.sha256(split_bytes).hexdigest() != manifest.get('split_manifest_hash'):
        raise ValueError('Trusted development split fingerprint mismatch')
    raw = Path(development).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != manifest.get('data_hash'):
        raise ValueError('Input is not the authorized development fingerprint; content was not parsed')
    split = json.loads(split_bytes)
    if split.get('data_hash') != actual:
        raise ValueError('Development and split fingerprints disagree')
    groups = [set(split.get(k, [])) for k in ('train', 'calibration', 'check')]
    if any(groups[i] & groups[j] for i in range(3) for j in range(i + 1, 3)):
        raise ValueError('Development split overlaps')
    records = json.loads(raw)
    if not isinstance(records, list):
        raise ValueError('Authorized development input must be a JSON array')
    indexes = split.get('calibration', [])
    if len(indexes) != len(set(indexes)) or any(type(i) is not int or i < 0 or i >= len(records) for i in indexes):
        raise ValueError('Invalid calibration indexes')
    selected = indexes[:limit] if limit is not None else indexes
    return [(i, records[i]) for i in selected], {
        'development_hash': actual, 'split_manifest_hash': hashlib.sha256(split_bytes).hexdigest(),
        'selection_split': 'development calibration only', 'calibration_population': len(indexes),
        'requested_limit': limit, 'selected_count': len(selected), 'partial': len(selected) < len(indexes),
    }


def _probability(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1:
        return float(value)
    return None


def _safe_result(result, sources):
    """Keep only typed probabilities and known source IDs, never ticket text or draft prose."""
    allowed = {(s.doc_id, s.chunk_id) for s in sources}
    passages = []
    for value in result.get('passages', []):
        if not isinstance(value, dict) or (value.get('doc_id'), value.get('chunk_id')) not in allowed:
            continue
        passages.append({'doc_id': value['doc_id'], 'chunk_id': value['chunk_id'],
                         'probability': _probability(value.get('probability')),
                         'status': value.get('status') if value.get('status') in ('pass', 'fail', 'unknown') else 'unknown'})
    return {'status': result.get('status') if result.get('status') in ('pass', 'fail', 'unknown') else 'unknown',
            'answerability_probability': _probability(result.get('answerability_probability')), 'passages': passages,
            'raw_model_scores': {k: _probability(v) for k, v in result.get('raw_answers', {}).items() if isinstance(k, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', k) and _probability(v) is not None},
            'reason_codes': [x for x in result.get('reasons', []) if isinstance(x, str) and re.fullmatch(r'[a-z0-9_]{1,100}', x)]}


def _provider_metadata(provider):
    allowed = ('configured_model', 'actual_model', 'actual_provider', 'verified', 'verified_at', 'catalog_hash',
               'last_call_id', 'last_http_status', 'last_retry_count', 'last_error', 'usage',
               'quota_wait_required_seconds', 'model_pricing', 'status', 'fixture')
    return {k: v for k, v in getattr(provider, 'metadata', {}).items() if k in allowed}


def summarize(rows):
    evaluated = [r for r in rows if r.get('baseline') is not None]
    answerable = [r for r in evaluated if r['reference']['answerable_from_docs'] is True]
    unsupported = [r for r in evaluated if r['reference']['answerable_from_docs'] is False]
    labelled = answerable + unsupported
    qualified = [r for r in labelled if r['baseline']['has_qualified_passage']]
    scored = [r for r in answerable if any(p['probability'] is not None for p in (r.get('decider') or {}).get('passages', []))]
    rank_hits = 0
    for row in scored:
        candidates = [p for p in row['decider']['passages'] if p['probability'] is not None]
        best = sorted(candidates, key=lambda p: (-p['probability'], p['doc_id'], p['chunk_id']))[0]
        rank_hits += best['doc_id'] in row['reference']['expected_doc_ids']
    rate = lambda n, d: n / d if d else None
    known = [r for r in labelled if (r.get('decider') or {}).get('status') in ('pass', 'fail')]
    return {
        'records_accounted': len(rows), 'local_retrieval_evaluated': len(evaluated),
        'remote_attempted_records': sum(r.get('remote_attempted', False) for r in rows),
        'live_inference_records': sum(r.get('live_inference_observed', False) for r in rows),
        'errors_or_unavailable': sum(r['status'] != 'evaluated' for r in rows),
        'baseline': {
            'answerable_denominator': len(answerable), 'no_support_denominator': len(unsupported),
            'raw_expected_doc_hit_at_4': rate(sum(r['baseline']['raw_expected_doc_hit'] for r in answerable), len(answerable)),
            'qualified_expected_doc_hit_at_4': rate(sum(r['baseline']['qualified_expected_doc_hit'] for r in answerable), len(answerable)),
            'answerability_recall_proxy': rate(sum(r['baseline']['has_qualified_passage'] for r in answerable), len(answerable)),
            'no_support_specificity_proxy': rate(sum(not r['baseline']['has_qualified_passage'] for r in unsupported), len(unsupported)),
            'selected_answerability_precision_proxy': rate(sum(r['reference']['answerable_from_docs'] is True for r in qualified), len(qualified)),
        },
        'decider': {
            'decision_coverage': rate(len(known), len(labelled)), 'decision_denominator': len(labelled),
            'unknown_decisions': sum((r.get('decider') or {}).get('status') not in ('pass', 'fail') for r in labelled),
            'scored_answerable_denominator': len(scored), 'top_scored_expected_doc_hit_at_1_proxy': rate(rank_hits, len(scored)),
            'semantic_grounding_accuracy': None, 'semantic_grounding_unavailable_reason': 'Expected document IDs do not establish passage sufficiency or claim entailment; reviewed semantic labels are required.',
        },
        'thresholds_selected': False,
        'limitations': ['Calibration cohort only; no supplied validation or held-out development check used.',
                        'Expected-document and answerability labels are proxies, not semantic truth.',
                        'Raw Decider probabilities are not asserted calibrated for CloudServe.',
                        'No release policy or threshold is automatically changed.'],
    }


def run(development, output, limit=None, *, settings=None, retriever=None, evaluator=None, sleep_fn=None):
    settings = settings or Settings()
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Output directory must be empty; retain earlier benchmark evidence')
    selected, selection = trusted_calibration(development, settings, limit)
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    manifest = {'schema_version': 1, 'started_at': now(), 'state': 'incomplete', **selection,
                'benchmark': 'Decider raw-top4 vs frozen cosine-floor development comparison',
                'cache_policy': 'No application benchmark response cache/replay; local embedding artifacts reused.',
                'requested_model': getattr(getattr(evaluator, 'provider', None), 'model', getattr(settings, 'decider_model', os.getenv('DECIDER_MODEL', 'perplexity/pplx-decider-flash-v1.3:free'))),
                'quota_sufficient': False, 'live_inference_observed': False, 'thresholds_selected': False,
                'source_version': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    from evaluation.harness import tree_hash
    manifest['code_hash'] = tree_hash(settings.root)
    rows = []
    provider = getattr(evaluator, 'provider', None)
    fixture = bool(getattr(provider, 'metadata', {}).get('fixture'))
    manifest['provenance'] = 'Fixture replay' if fixture else 'Live development benchmark'
    _write(output / 'manifest.json', manifest)

    def blocked(reason):
        manifest.update(state='blocked', blocker=reason)
        for index, original in selected:
            rows.append({'input_index': index, 'input_hash': digest(original), 'status': 'not_run',
                         'reason': reason, 'baseline': None, 'decider': None, 'remote_attempted': False,
                         'live_inference_observed': False, 'provenance': manifest['provenance']})

    try:
        if not settings.api_key and evaluator is None:
            blocked('local_openrouter_key_missing')
        else:
            if evaluator is None:
                from src.providers.decider import DeciderProvider
                from src.decider import DeciderEvaluator
                policy = json.loads((settings.root / 'policies/decider.json').read_text())
                provider = DeciderProvider(api_key=settings.api_key, model=manifest['requested_model'], timeout=float(os.getenv('DECIDER_TIMEOUT_SECONDS', os.getenv('MODEL_TIMEOUT_SECONDS', '30'))), max_retries=int(os.getenv('DECIDER_MAX_RETRIES', os.getenv('MODEL_MAX_RETRIES', '2'))))
                evaluator = DeciderEvaluator(provider, policy)
                fixture = bool(getattr(provider, 'metadata', {}).get('fixture'))
                manifest['provenance'] = 'Fixture replay' if fixture else 'Live development benchmark'
            try:
                preflight = provider.preflight()
                manifest['provider_preflight'] = preflight
                fixture = fixture or preflight.get('fixture') is True
                manifest['provenance'] = 'Fixture replay' if fixture else 'Live development benchmark'
                if not preflight.get('verified'):
                    raise RuntimeError('Unverified provider')
            except Exception as exc:
                manifest['preflight_error_class'] = type(exc).__name__
                blocked('free_provider_preflight_failed')
            if manifest['state'] != 'blocked':
                remaining = (preflight.get('quota') or {}).get('remaining_free_requests')
                quota_verified = (preflight.get('quota') or {}).get('free_request_allowance_verified') is True
                max_calls = len(selected) * (1 + int(getattr(provider, 'max_retries', 2)))
                sufficient = type(remaining) is int and remaining >= max_calls and quota_verified
                manifest.update(estimated_max_requests=max_calls, remaining_free_requests=remaining if quota_verified else None,
                                quota_sufficient=sufficient, quota_note='Unknown allowance blocks a full benchmark; an explicit --limit permits only a bounded development smoke, not a quota-sufficiency claim.')
                if not selection['partial'] and not sufficient:
                    blocked('full_benchmark_free_quota_unverified_or_insufficient')
                else:
                    if retriever is None:
                        from src.retrieve import Retriever
                        retriever = Retriever(settings.corpus_path, settings.chroma_path)
                    if not retriever.ready:
                        blocked('frozen_retrieval_assets_unavailable')
                    else:
                        manifest['retrieval_artifacts'] = retriever.metadata
                        manifest['evaluator_fingerprint'] = evaluator.fingerprint() if hasattr(evaluator, 'fingerprint') else None
                        manifest['semantic_policy_calibration_ready'] = bool(getattr(evaluator, 'policy_ready', False))
                        circuit = None
                        last_attempt = None
                        for index, original in selected:
                            row = {'input_index': index, 'input_hash': digest(original), 'status': 'unavailable',
                                   'baseline': None, 'decider': None, 'remote_attempted': False,
                                   'live_inference_observed': False, 'provenance': manifest['provenance']}
                            tick = time.monotonic()
                            before_call = getattr(provider, 'metadata', {}).get('last_call_id')
                            try:
                                ticket = normalize(original, 'benchmark', 'benchmark', index)
                                if not ticket.valid:
                                    raise ValueError('Authorized development record invalid')
                                labels = original.get('labels', {})
                                expected = labels.get('expected_doc_ids', [])
                                row['reference'] = {'answerable_from_docs': labels.get('answerable_from_docs'), 'expected_doc_ids': expected}
                                qualified = retriever.search(ticket.subject + '\n' + ticket.body, k=4)
                                raw = list(getattr(retriever, 'last_candidates', qualified))[:4]
                                row['baseline'] = {'has_qualified_passage': bool(qualified),
                                                   'raw_expected_doc_hit': bool(set(expected) & {s.doc_id for s in raw}),
                                                   'qualified_expected_doc_hit': bool(set(expected) & {s.doc_id for s in qualified}),
                                                   'raw_candidates': [{'doc_id': s.doc_id, 'chunk_id': s.chunk_id, 'score': s.score} for s in raw],
                                                   'qualified_chunk_ids': [s.chunk_id for s in qualified]}
                                if circuit:
                                    row['reason'] = circuit
                                else:
                                    if last_attempt is not None and not fixture:
                                        (sleep_fn or time.sleep)(max(0.0, 3.1 - (time.monotonic() - last_attempt)))
                                    before_call = getattr(provider, 'metadata', {}).get('last_call_id')
                                    last_attempt = time.monotonic()
                                    result = evaluator.retrieval(ticket, raw)
                                    row['decider'] = _safe_result(result, raw)
                                    metadata = _provider_metadata(provider)
                                    row['provider'] = metadata
                                    row['remote_attempted'] = bool(metadata.get('last_call_id') and metadata.get('last_call_id') != before_call)
                                    row['live_inference_observed'] = bool(not fixture and result.get('raw_answers') and metadata.get('actual_model') and row['remote_attempted'])
                                    row['status'] = 'evaluated' if result.get('raw_answers') else 'unavailable'
                                    if row['status'] == 'unavailable':
                                        row['reason'] = 'semantic_evaluator_unavailable_or_no_typed_answers'
                                    if metadata.get('last_http_status') == 429 or metadata.get('quota_wait_required_seconds'):
                                        circuit = 'quota_exhausted_remaining_records_not_called'
                            except Exception as exc:
                                row['error_class'] = type(exc).__name__
                                row['reason'] = 'benchmark_record_unavailable'
                                metadata = _provider_metadata(provider)
                                row['provider'] = metadata
                                row['remote_attempted'] = bool(metadata.get('last_call_id') and metadata.get('last_call_id') != before_call)
                                if metadata.get('last_http_status') == 429 or metadata.get('quota_wait_required_seconds'):
                                    circuit = 'quota_exhausted_remaining_records_not_called'
                            row['elapsed_seconds'] = time.monotonic() - tick
                            rows.append(row)
                            with (output / 'results.jsonl').open('a', encoding='utf-8') as f:
                                f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n'); f.flush(); os.fsync(f.fileno())
                        manifest['state'] = 'completed' if all(r['status'] == 'evaluated' for r in rows) else 'completed_with_unavailable_results'
    except BaseException as exc:
        manifest['state'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'incomplete'
        manifest['error_class'] = type(exc).__name__
        raise
    finally:
        if not (output / 'results.jsonl').exists():
            (output / 'results.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in rows), encoding='utf-8')
        summary = summarize(rows)
        manifest.update(finished_at=now(), elapsed_seconds=time.monotonic() - started,
                        records_accounted=len(rows), live_inference_observed=any(r['live_inference_observed'] for r in rows))
        _write(output / 'summary.json', summary); _write(output / 'manifest.json', manifest)
    return manifest, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--development', required=True); parser.add_argument('--output', required=True)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    manifest, summary = run(args.development, args.output, args.limit)
    print(json.dumps({'state': manifest['state'], 'records_accounted': manifest['records_accounted'],
                      'live_inference_observed': manifest['live_inference_observed'], 'blocker': manifest.get('blocker')}))
    if manifest['state'] != 'completed':
        raise SystemExit(2 if manifest['state'] == 'blocked' else 1)

if __name__ == '__main__':
    main()
