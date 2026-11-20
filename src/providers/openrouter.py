"""Free-only text generation with per-call evidence and no model fallback."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

import httpx
from pydantic import ValidationError


class ProviderUnavailable(RuntimeError):
    pass


class FreePolicyError(ProviderUnavailable):
    pass


class OpenRouterProvider:
    reasoning_policy = "disable_if_catalog_explicitly_optional_v1"
    _pacing_lock = threading.Lock()
    _last_attempt = None
    minimum_attempt_interval_seconds = 3.1
    _call_fields = ('actual_model', 'actual_provider', 'usage', 'last_call_at', 'response_id',
                    'last_finish_reason', 'last_error', 'last_http_status', 'quota_wait_required_seconds',
                    'last_prompt_hash', 'last_request_reasoning', 'last_retry_count', 'last_call_id', 'last_task', 'last_call_status')

    def __init__(self, api_key='', model='google/gemma-4-26b-a4b-it:free', timeout=30, max_retries=2, free_only=True, **kwargs):
        self.api_key, self.model, self.free_only = api_key, model, free_only
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('Timeout must be finite and positive')
        self.max_retries = min(2, max(0, int(max_retries)))
        self.client = httpx.Client(base_url='https://openrouter.ai/api/v1', timeout=self.timeout)
        self._lock = threading.Semaphore(1)
        self._verified = False
        self._cost_violation = False
        self.providers = []
        self._model_ids = self._aliases({model})
        self.response_format = False
        self.temperature = False
        self.reasoning_config = None
        self.metadata = {'configured_model': model, 'provider': 'OpenRouter', 'verified': False,
                         'catalog_verified': False, 'reasoning_policy': self.reasoning_policy, 'status': 'missing_key' if not api_key else 'not_preflighted',
                         'prompt_versions': {'PR-03':'1.3','PR-04':'1.2','PR-05':'1.2'},
                         'request_attempts': 0, 'successful_http_responses': 0, 'successful_calls': 0, 'calls': 0}

    @staticmethod
    def _aliases(ids):
        result = set()
        for model_id in ids:
            if isinstance(model_id, str) and re.fullmatch(r'[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+', model_id):
                result.add(model_id)
                if model_id.endswith(':free'):
                    result.add(model_id[:-5])
        return result

    def _free(self, pricing):
        if not isinstance(pricing, dict) or not {'prompt', 'completion'} <= set(pricing):
            return False
        for key, value in pricing.items():
            if key == 'discount':
                continue
            try:
                amount = Decimal(str(value))
                if not amount.is_finite() or amount != 0:
                    return False
            except (InvalidOperation, ValueError, TypeError):
                return False
        return True

    def preflight(self):
        if self._cost_violation:
            raise FreePolicyError('Provider stopped after unknown or nonzero usage cost')
        self._verified = False
        self.metadata.update(verified=False, catalog_verified=False)
        if not self.free_only or not self.model.endswith(':free') or self.model in ('openrouter/free', 'openrouter/auto') or self.model.startswith('~'):
            self.metadata['status'] = 'free_policy_rejected'
            raise FreePolicyError('Explicit free model required')
        if not self.api_key:
            self.metadata['status'] = 'missing_key'
            raise ProviderUnavailable('OpenRouter key missing')
        headers = {'Authorization': 'Bearer ' + self.api_key}
        try:
            response = self.client.get('/models', headers=headers)
            response.raise_for_status()
            model = next((entry for entry in response.json()['data'] if entry.get('id') == self.model), None)
            if not model or not self._free(model.get('pricing')):
                raise FreePolicyError('Model pricing is not explicitly zero')
            architecture = model.get('architecture', {})
            if 'text' not in architecture.get('input_modalities', []) or 'text' not in architecture.get('output_modalities', []):
                raise FreePolicyError('Text input/output unavailable')
            response = self.client.get('/models/' + self.model + '/endpoints', headers=headers)
            response.raise_for_status()
            endpoints = response.json().get('data', {}).get('endpoints', [])
            free = [entry for entry in endpoints if self._free(entry.get('pricing')) and isinstance(entry.get('provider_name'), str) and entry['provider_name']]
            if not free:
                raise FreePolicyError('No verified zero-price endpoint')
            self.providers = sorted({entry['provider_name'] for entry in free})
            advertised = {self.model, model.get('canonical_slug')}
            for endpoint in free:
                advertised.update((endpoint.get('model_id'), endpoint.get('model')))
                name = endpoint.get('name')
                if isinstance(name, str) and '|' in name:
                    advertised.add(name.rsplit('|', 1)[1].strip())
            self._model_ids = self._aliases(advertised)
            params = set(model.get('supported_parameters', []))
            self.response_format = 'response_format' in params and all('response_format' in entry.get('supported_parameters', params) for entry in free)
            self.temperature = 'temperature' in params
            reasoning = model.get('reasoning')
            mandatory = reasoning.get('mandatory') if isinstance(reasoning, dict) else None
            # Do not infer optionality from a name, default effort, or absent field.
            self.reasoning_config = {'enabled': False} if mandatory is False and 'reasoning' in params else None
            quota = {}
            try:
                response = self.client.get('/key', headers=headers)
                response.raise_for_status()
                data = response.json().get('data', {})
                quota = {key: data.get(key) for key in ('is_free_tier', 'limit', 'limit_remaining', 'usage', 'usage_daily', 'rate_limit')}
            except Exception:
                quota = {'status': 'unavailable'}
            self.metadata.update(verified=True, catalog_verified=True, preflight_status='verified',
                                 status=self.metadata.get('status') if self.metadata.get('last_call_status') in ('success', 'failed') else 'verified',
                                 verified_at=datetime.now(timezone.utc).isoformat(),
                                 catalog_hash=hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest(),
                                 model_pricing=model['pricing'],
                                 verified_endpoints=[{'provider': e['provider_name'], 'pricing': e['pricing'], 'name': e.get('name')} for e in free],
                                 verified_model_ids=sorted(self._model_ids), quota=quota,
                                 response_format=self.response_format, strict_schema=False,
                                 reasoning_policy=self.reasoning_policy,
                                 reasoning_mandatory=mandatory if isinstance(mandatory, bool) else None,
                                 reasoning_request_config=dict(self.reasoning_config) if self.reasoning_config is not None else None)
            self._verified = True
            return dict(self.metadata)
        except FreePolicyError:
            self.metadata['status'] = 'free_policy_rejected'
            raise
        except Exception:
            self.metadata['status'] = 'preflight_unavailable'
            raise ProviderUnavailable('Provider preflight unavailable') from None

    def _reset_call(self, task):
        for key in self._call_fields:
            self.metadata.pop(key, None)
        self.metadata.update(last_call_id=str(uuid.uuid4()), last_task=task, last_retry_count=0,
                             last_error=None, last_http_status=None, last_call_status='pending', status='request_pending')

    def _failure(self, code):
        self.metadata.update(last_error=code, last_call_status='failed',
                             status='cost_policy_violation' if self._cost_violation else code)

    def _cost_failure(self, code):
        self._cost_violation = True
        self._verified = False
        self.metadata.update(verified=False)
        self.metadata['cost_violation_evidence']={key:self.metadata.get(key) for key in ('last_call_id','actual_model','actual_provider','usage','response_id')}
        self._failure(code)
        raise FreePolicyError('Reported usage cost is not explicitly zero')

    def _capture_usage(self, data):
        raw = data.get('usage')
        if not isinstance(raw, dict):
            self.metadata['usage'] = None
            self._cost_failure('usage_cost_missing')
        safe = {}
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if key in raw and isinstance(raw[key], int) and not isinstance(raw[key], bool) and raw[key] >= 0:
                safe[key] = raw[key]
        safe['cost'] = None
        self.metadata['usage'] = safe
        if 'cost' not in raw:
            self._cost_failure('usage_cost_missing')
        try:
            if isinstance(raw['cost'], bool):
                raise ValueError('Boolean cost')
            cost = Decimal(str(raw['cost']))
            if not cost.is_finite() or cost < 0:
                raise ValueError('Invalid cost')
        except (InvalidOperation, TypeError, ValueError):
            self._cost_failure('usage_cost_invalid')
        # Preserve nonzero decimal evidence exactly; float conversion could
        # underflow a charged amount to zero or overflow it to infinity.
        safe['cost'] = 0.0 if cost == 0 else str(cost)
        if cost != 0:
            self._cost_failure('unexpected_nonzero_cost')
        # Missing usage counts remain unavailable; do not invent zero token use.
        if not {'prompt_tokens', 'completion_tokens'} <= set(safe):
            self._failure('usage_invalid')
            raise ProviderUnavailable('Provider usage counts unavailable')

    def _pace(self):
        with OpenRouterProvider._pacing_lock:
            previous = OpenRouterProvider._last_attempt
            if previous is not None:
                delay = self.minimum_attempt_interval_seconds - (time.monotonic() - previous)
                if delay > 0:
                    time.sleep(delay)
            OpenRouterProvider._last_attempt = time.monotonic()

    @staticmethod
    def _retry_after(value):
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                raise ValueError('Retry delay unavailable') from None
        if not math.isfinite(delay):
            raise ValueError('Retry delay unavailable')
        return max(0, delay)

    def _routing_price_limits(self):
        # Production requests always retain zero-price routing.
        return {'prompt':0,'completion':0}

    def _before_request(self,request):
        # Isolated, explicitly authorized test adapters may reserve a budget.
        return None

    def complete(self, task, payload, schema, max_tokens):
        with self._lock:
            self._reset_call(task)
            if self._cost_violation:
                self._failure('cost_policy_latched')
                raise FreePolicyError('Provider stopped after unknown or nonzero usage cost')
            try:
                if not self._verified:
                    self.preflight()
            except FreePolicyError:
                self._failure('free_policy_rejected')
                raise
            except ProviderUnavailable:
                self._failure('missing_key' if not self.api_key else 'preflight_unavailable')
                raise
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 32768:
                self._failure('configuration_rejected')
                raise ValueError('Token budget must be an integer in the supported range')
            try:
                prompt_path = Path(__file__).resolve().parents[2] / 'prompts/build' / ('PR-' + {'assess': '03', 'draft': '04', 'validate': '05'}.get(task, '06') + '.txt')
                prompt = prompt_path.read_text()
                prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
                self.metadata.setdefault('prompt_hashes', {})[task] = prompt_hash
                self.metadata['last_prompt_hash'] = prompt_hash
                request = {'model': self.model, 'messages': [
                    {'role': 'system', 'content': prompt + '\nReturn ONLY a JSON object matching this schema: ' + json.dumps(schema.model_json_schema())},
                    {'role': 'user', 'content': json.dumps({'untrusted_data': payload}, ensure_ascii=False, allow_nan=False)}],
                    'max_tokens': max_tokens,
                    'provider': {'only': self.providers, 'allow_fallbacks': False, 'max_price': self._routing_price_limits()}}
                self.metadata['last_request_reasoning'] = dict(self.reasoning_config) if self.reasoning_config is not None else None
                if self.reasoning_config is not None:
                    request['reasoning'] = dict(self.reasoning_config)
                if self.temperature:
                    request['temperature'] = 0
                if self.response_format:
                    request['response_format'] = {'type': 'json_object'}
            except Exception:
                self._failure('configuration_rejected')
                raise ProviderUnavailable('Generation request configuration invalid') from None
            for attempt in range(self.max_retries + 1):
                self.metadata['last_retry_count'] = attempt
                try:
                    self._before_request(request)
                    self._pace()
                    self.metadata['request_attempts'] += 1
                    response = self.client.post('/chat/completions', headers={'Authorization': 'Bearer ' + self.api_key}, json=request)
                    self.metadata['last_http_status'] = response.status_code
                    if response.status_code in (400, 401, 402, 403, 404, 422):
                        self._failure('configuration_or_authorization_rejected')
                        raise ProviderUnavailable('Provider configuration or authorization rejected')
                    if response.status_code == 429:
                        self._failure('quota_exhausted')
                        if attempt == self.max_retries:
                            raise ProviderUnavailable('Provider quota exhausted')
                        try:
                            delay = self._retry_after(response.headers.get('retry-after', '3'))
                        except ValueError:
                            self._failure('quota_retry_delay_unavailable')
                            raise ProviderUnavailable('Provider quota retry delay unavailable') from None
                        if delay > 30:
                            self.metadata['quota_wait_required_seconds'] = delay
                            self._failure('quota_wait_required')
                            raise ProviderUnavailable('Provider quota wait exceeds bounded retry window')
                        time.sleep(delay)
                        continue
                    if response.status_code >= 500:
                        self._failure('provider_unavailable')
                        if attempt == self.max_retries:
                            raise ProviderUnavailable('Provider temporarily unavailable')
                        time.sleep(min(2 ** attempt, 4))
                        continue
                    if response.status_code != 200:
                        self._failure('provider_http_error')
                        raise ProviderUnavailable('Provider request rejected')
                    self.metadata['successful_http_responses'] += 1
                    try:
                        data = response.json()
                    except Exception:
                        self._failure('response_schema_invalid')
                        raise ProviderUnavailable('Provider response invalid') from None
                    if not isinstance(data, dict):
                        self._failure('response_schema_invalid')
                        raise ProviderUnavailable('Provider response invalid')
                    actual_model, actual_provider = data.get('model'), data.get('provider')
                    if actual_model not in self._model_ids or actual_provider not in self.providers:
                        self._failure('response_identity_unverified')
                        raise ProviderUnavailable('Returned model or provider identity unverified')
                    self.metadata.update(actual_model=actual_model, actual_provider=actual_provider,
                                         last_call_at=datetime.now(timezone.utc).isoformat())
                    response_id = data.get('id')
                    if isinstance(response_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', response_id):
                        self.metadata['response_id'] = response_id
                    self._capture_usage(data)
                    choices = data.get('choices')
                    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                        self._failure('response_schema_invalid')
                        raise ProviderUnavailable('Provider response invalid')
                    choice = choices[0]
                    finish = choice.get('finish_reason')
                    self.metadata['last_finish_reason'] = finish if finish in ('stop', 'length', 'content_filter', 'tool_calls', 'error') else 'unknown'
                    if finish != 'stop':
                        self._failure('provider_refusal' if finish == 'content_filter' else 'incomplete_output')
                        raise ProviderUnavailable('Provider output incomplete or refused')
                    message = choice.get('message')
                    if not isinstance(message, dict) or message.get('refusal') or message.get('tool_calls'):
                        self._failure('provider_refusal')
                        raise ProviderUnavailable('Provider output refused or outside expected format')
                    content = message.get('content')
                    if not isinstance(content, str) or not content.strip():
                        self._failure('incomplete_output')
                        raise ProviderUnavailable('Provider output empty')
                    try:
                        result = schema.model_validate_json(content)
                    except (ValidationError, ValueError, TypeError):
                        self._failure('response_schema_invalid')
                        raise ProviderUnavailable('Provider structured output invalid') from None
                    self.metadata['successful_calls'] += 1
                    self.metadata['calls'] = self.metadata['successful_calls']
                    self.metadata.update(last_error=None, last_call_status='success', status='available')
                    return result
                except (httpx.TimeoutException, httpx.TransportError):
                    self._failure('network_timeout')
                    if attempt == self.max_retries:
                        raise ProviderUnavailable('Provider network timeout') from None
                    time.sleep(min(2 ** attempt, 4))
                except ProviderUnavailable:
                    raise
                except Exception:
                    self._failure('response_schema_invalid')
                    raise ProviderUnavailable('Provider response invalid') from None
            self._failure('attempts_exhausted')
            raise ProviderUnavailable('Provider attempts exhausted')
