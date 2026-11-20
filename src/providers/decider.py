"""Free-only OpenRouter Decisions adapter. No chat completions or fallback models.

Contract source: https://openrouter.ai/docs/guides/features/decisions
Probabilities are provider model scores, not CloudServe calibration evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
import threading
import time
import uuid

import httpx

from .openrouter import ProviderUnavailable, FreePolicyError


class DeciderUnavailable(ProviderUnavailable):
    """No usable decision evidence; caller must return unknown/escalate."""


def _number(value, lower=0.0, upper=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Expected numeric score')
    value = float(value)
    if not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError('Score outside permitted range')
    return value


def _distribution(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('Probability labels differ from requested criteria')
    result = {key: _number(probability) for key, probability in value.items()}
    if not math.isclose(sum(result.values()), 1.0, abs_tol=0.001, rel_tol=0):
        raise ValueError('Probability distribution does not sum to one')
    return result


def validate_questions(questions):
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 128:
        raise ValueError('One to 128 typed questions required')
    for key, question in questions.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key):
            raise ValueError('Invalid question identifier')
        if not isinstance(question, dict) or set(question) - {'type', 'instructions', 'criteria'}:
            raise ValueError('Unexpected question fields')
        if not isinstance(question.get('instructions'), str) or not question['instructions'].strip():
            raise ValueError('Question instructions required')
        kind = question.get('type')
        criteria = question.get('criteria')
        if kind == 'noul':
            if criteria is not None and (not isinstance(criteria, dict) or set(criteria) != {'true', 'false'}):
                raise ValueError('Binary criteria must define true and false')
        elif kind == 'choice':
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255 or not all(isinstance(x, str) and x for x in criteria):
                raise ValueError('Choice criteria required')
        elif kind == 'score':
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 255:
                raise ValueError('Ordered score criteria required')
        else:
            raise ValueError('Unsupported question type')
    # Reject non-JSON values/nonfinite nested data before any provider request.
    json.dumps(questions, allow_nan=False)


def validate_answers(answers, questions):
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError('Missing or unexpected decision answers')
    clean = {}
    for key, question in questions.items():
        answer = answers[key]
        kind = question['type']
        if not isinstance(answer, dict) or answer.get('type') != kind:
            raise ValueError('Question refused or returned an unexpected type')
        allowed = {'type', 'confidence', kind}
        if kind in ('choice', 'score'):
            allowed.add('probabilities')
        if set(answer) - allowed:
            raise ValueError('Unexpected answer fields')
        value = {'type': kind}
        if 'confidence' in answer:
            value['confidence'] = _number(answer['confidence'])
        if kind == 'noul':
            value['noul'] = _number(answer.get('noul'))
        elif kind == 'choice':
            value['probabilities'] = _distribution(answer.get('probabilities'), question['criteria'])
            choice = answer.get('choice')
            if choice not in question['criteria'] or value['probabilities'][choice] < max(value['probabilities'].values()) - 1e-9:
                raise ValueError('Choice is not a highest-probability option')
            value['choice'] = choice
        else:
            count = len(question['criteria'])
            value['probabilities'] = _distribution(answer.get('probabilities'), [str(i) for i in range(count)])
            value['score'] = _number(answer.get('score'), 0, count - 1)
            expected = sum(int(k) * v for k, v in value['probabilities'].items())
            if not math.isclose(value['score'], expected, abs_tol=0.01, rel_tol=0):
                raise ValueError('Score does not match its distribution')
        clean[key] = value
    return clean


class DeciderProvider:
    _pacing_lock = threading.Lock()
    _last_attempt = None
    minimum_attempt_interval_seconds = 3.1

    def __init__(self, api_key='', model='perplexity/pplx-decider-flash-v1.3:free', timeout=30, max_retries=2):
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('Timeout must be finite and positive')
        if isinstance(max_retries, bool) or int(max_retries) < 0:
            raise ValueError('Retries must be nonnegative')
        self.max_retries = min(2, int(max_retries))
        self.client = httpx.Client(base_url='https://openrouter.ai', timeout=self.timeout)
        self._verified = False
        self._cost_violation = False
        self._lock = threading.Semaphore(1)
        self.providers = []
        self._model_ids = {model}
        self.metadata = {'configured_model': model, 'provider': 'OpenRouter', 'api': 'decisions',
                         'verified': False, 'status': 'missing_key' if not api_key else 'not_preflighted',
                         'score_calibration': 'provider_scores_not_CloudServe_calibrated'}

    @staticmethod
    def _free(pricing):
        if not isinstance(pricing, dict) or not {'prompt', 'completion'} <= set(pricing):
            return False
        for name, value in pricing.items():
            if name == 'discount':
                continue
            try:
                number = Decimal(str(value))
                if not number.is_finite() or number != 0:
                    return False
            except (InvalidOperation, ValueError, TypeError):
                return False
        return True

    def preflight(self):
        if self._cost_violation:
            raise FreePolicyError('Decisions provider stopped after nonzero usage')
        self._verified = False
        self.metadata.update(verified=False)
        if not self.model.endswith(':free') or self.model in ('openrouter/free', 'openrouter/auto') or self.model.startswith('~'):
            raise FreePolicyError('An explicit free Decisions variant is required')
        if not self.api_key:
            raise DeciderUnavailable('OpenRouter key missing')
        headers = {'Authorization': 'Bearer ' + self.api_key}
        try:
            response = self.client.get('/api/v1/models', params={'output_modalities': 'decisions'}, headers=headers)
            response.raise_for_status()
            entry = next((x for x in response.json()['data'] if x.get('id') == self.model), None)
            if not entry or not self._free(entry.get('pricing')):
                raise FreePolicyError('Decisions model pricing is not explicitly zero')
            architecture = entry.get('architecture', {})
            if 'text' not in architecture.get('input_modalities', []) or 'decisions' not in architecture.get('output_modalities', []):
                raise FreePolicyError('Text-input Decisions capability unavailable')
            response = self.client.get('/api/v1/models/' + self.model + '/endpoints', headers=headers)
            response.raise_for_status()
            endpoints = response.json().get('data', {}).get('endpoints', [])
            free = [x for x in endpoints if self._free(x.get('pricing')) and isinstance(x.get('provider_name'), str) and x['provider_name']]
            if not free:
                raise FreePolicyError('No verified zero-price Decisions endpoint')
            self.providers = sorted({x['provider_name'] for x in free})
            self._model_ids = {self.model} | {x for x in [entry.get('canonical_slug')] + [e.get('model_id') for e in free] if isinstance(x, str) and x}
            quota = {'status': 'unavailable', 'remaining_free_requests': None, 'free_request_allowance_verified': False}
            try:
                q = self.client.get('/api/v1/key', headers=headers)
                q.raise_for_status()
                data = q.json().get('data', {})
                quota.update({k: data.get(k) for k in ('is_free_tier', 'limit', 'limit_remaining', 'usage', 'usage_daily', 'rate_limit')})
                remaining = data.get('remaining_free_requests')
                # An explicitly exposed request count is evidence; dollar balance is not.
                if isinstance(remaining, int) and not isinstance(remaining, bool) and remaining >= 0:
                    quota.update(status='evidenced_request_allowance', remaining_free_requests=remaining, free_request_allowance_verified=True)
            except Exception:
                pass
            self.metadata.update(verified=True, status='verified', verified_at=datetime.now(timezone.utc).isoformat(),
                                 catalog_hash=hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest(),
                                 model_pricing=entry['pricing'], verified_endpoints=[{'provider': e['provider_name'], 'pricing': e['pricing']} for e in free], quota=quota)
            self._verified = True
            return dict(self.metadata)
        except FreePolicyError:
            self.metadata['status'] = 'free_policy_rejected'
            raise
        except Exception:
            self.metadata['status'] = 'preflight_unavailable'
            raise DeciderUnavailable('Decisions preflight unavailable') from None

    @staticmethod
    def _retry_after(header):
        try:
            delay = float(header)
        except (ValueError, TypeError):
            try:
                delay = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                raise DeciderUnavailable('Provider retry delay unavailable') from None
        if not math.isfinite(delay):
            raise DeciderUnavailable('Provider retry delay unavailable')
        return max(0, delay)

    def _pace(self):
        with DeciderProvider._pacing_lock:
            previous = DeciderProvider._last_attempt
            if previous is not None:
                delay = self.minimum_attempt_interval_seconds - (time.monotonic() - previous)
                if delay > 0:
                    time.sleep(delay)
            DeciderProvider._last_attempt = time.monotonic()

    def decide(self, state, questions):
        if not isinstance(state, dict):
            raise ValueError('Decisions state must be an allowlisted object')
        validate_questions(questions)
        json.dumps(state, allow_nan=False)
        with self._lock:
            if self._cost_violation:
                raise FreePolicyError('Decisions provider stopped after nonzero usage')
            self.metadata.update(last_call_id=str(uuid.uuid4()), last_retry_count=0, last_error=None,
                                 last_http_status=None, last_task='decisions')
            if not self._verified:
                self.preflight()
            payload = {'model': self.model, 'state': state, 'questions': questions,
                       'provider': {'only': self.providers, 'allow_fallbacks': False, 'max_price': {'prompt': 0, 'completion': 0}}}
            for attempt in range(self.max_retries + 1):
                self.metadata['last_retry_count'] = attempt
                try:
                    self._pace()
                    self.metadata['request_attempts'] = self.metadata.get('request_attempts', 0) + 1
                    response = self.client.post('/api/alpha/decisions', headers={'Authorization': 'Bearer ' + self.api_key}, json=payload)
                    self.metadata['last_http_status'] = response.status_code
                    if response.status_code in (400, 401, 402, 403, 404, 422):
                        self.metadata['last_error'] = 'configuration_or_authorization'
                        raise DeciderUnavailable('Decisions configuration or authorization rejected')
                    if response.status_code == 429:
                        self.metadata['last_error'] = 'quota_exhausted'
                        if attempt == self.max_retries:
                            raise DeciderUnavailable('Decisions quota exhausted')
                        delay = self._retry_after(response.headers.get('retry-after', '3'))
                        if delay > 30:
                            self.metadata['quota_wait_required_seconds'] = delay
                            raise DeciderUnavailable('Decisions quota wait exceeds bounded retry window')
                        time.sleep(delay)
                        continue
                    if response.status_code >= 500:
                        self.metadata['last_error'] = 'provider_unavailable'
                        if attempt == self.max_retries:
                            raise DeciderUnavailable('Decisions provider unavailable')
                        time.sleep(min(2 ** attempt, 4))
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError('Invalid response envelope')
                    actual_model, actual_provider = data.get('model'), data.get('provider')
                    if actual_model not in self._model_ids or actual_provider not in self.providers:
                        raise ValueError('Unverified returned model or provider')
                    if not isinstance(data.get('id'), str) or not data['id']:
                        raise ValueError('Response identifier unavailable')
                    usage = data.get('usage')
                    if not isinstance(usage, dict):
                        raise ValueError('Usage evidence unavailable')
                    for key in ('input_tokens', 'output_tokens'):
                        if not isinstance(usage.get(key), int) or isinstance(usage[key], bool) or usage[key] < 0:
                            raise ValueError('Invalid usage count')
                    cost = Decimal(str(usage.get('cost')))
                    if not cost.is_finite():
                        raise ValueError('Usage cost unavailable')
                    self.metadata.update(actual_model=actual_model, actual_provider=actual_provider,
                                         usage={'input_tokens': usage['input_tokens'], 'output_tokens': usage['output_tokens'], 'cost': float(cost)},
                                         last_call_at=datetime.now(timezone.utc).isoformat())
                    if cost != 0:
                        self.metadata['last_error'] = 'unexpected_nonzero_cost'
                        self._cost_violation = True
                        self._verified = False
                        self.metadata.update(verified=False, status='cost_policy_violation')
                        raise DeciderUnavailable('Nonzero Decisions usage rejected')
                    answers = validate_answers(data.get('answers'), questions)
                    self.metadata['calls'] = self.metadata.get('calls', 0) + 1
                    self.metadata['last_error'] = None
                    return {'id': data['id'], 'model': actual_model, 'provider': actual_provider,
                            'answers': answers, 'usage': self.metadata['usage']}
                except (httpx.TimeoutException, httpx.TransportError):
                    self.metadata['last_error'] = 'network_timeout'
                    if attempt == self.max_retries:
                        raise DeciderUnavailable('Decisions network timeout') from None
                    time.sleep(min(2 ** attempt, 4))
                except DeciderUnavailable:
                    raise
                except Exception:
                    self.metadata['last_error'] = 'invalid_response'
                    raise DeciderUnavailable('Decisions response invalid') from None
            raise DeciderUnavailable('Decisions attempts exhausted')
