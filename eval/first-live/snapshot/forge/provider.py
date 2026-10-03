from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Provider:
    """One recorded transport attempt per model step; no hidden retry multiplier."""
    def __init__(self, config):
        self.config = config
        try:
            url = urllib.parse.urlsplit(config.base_url)
            local = url.hostname == 'localhost'
            if url.hostname and not local:
                try:
                    local = ipaddress.ip_address(url.hostname).is_loopback
                except ValueError:
                    pass
            if (not url.hostname or url.username or url.password or url.query or url.fragment
                    or url.scheme not in {'https', 'http'} or (url.scheme == 'http' and not local)):
                raise ValueError
            url.port
        except ValueError:
            raise ProviderError('invalid_base_url') from None
        self.url = config.base_url.rstrip('/') + '/chat/completions'
        self.opener = urllib.request.build_opener(NoRedirect())

    def complete(self, messages: list, tools: list) -> dict:
        body = {'model': self.config.model, 'messages': messages, 'tools': tools,
                'tool_choice': 'required', 'parallel_tool_calls': False,
                'temperature': 0, 'max_tokens': 4096}
        # DeepSeek's current non-thinking tool protocol; other providers receive
        # only common Chat Completions fields.
        if urllib.parse.urlsplit(self.url).hostname == 'api.deepseek.com':
            body['thinking'] = {'type': 'disabled'}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                    headers={'Authorization': 'Bearer ' + self.config.api_key,
                             'Content-Type': 'application/json'}, method='POST')
        try:
            with self.opener.open(request, timeout=60) as response:
                data = response.read(1_048_577)
            if len(data) > 1_048_576:
                raise ProviderError('model_response_too_large')
            payload = json.loads(data)
        except urllib.error.HTTPError as error:
            raise ProviderError(f'provider_http_{error.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError('provider_connection_failed') from None
        except (ValueError, UnicodeError):
            raise ProviderError('provider_invalid_json') from None
        try:
            msg = payload['choices'][0]['message']
            if not isinstance(msg, dict) or msg.get('role') != 'assistant':
                raise ValueError
            calls = msg.get('tool_calls') or []
            if not isinstance(calls, list) or len(calls) > 16:
                raise ValueError
            seen = set()
            for call in calls:
                if (not isinstance(call, dict) or call.get('type') != 'function'
                        or not isinstance(call.get('id'), str) or call['id'] in seen
                        or not isinstance(call['function']['name'], str)
                        or not isinstance(call['function']['arguments'], str)):
                    raise ValueError
                seen.add(call['id'])
            usage = payload.get('usage') or {}
            total = usage.get('total_tokens')
            if total is not None and (type(total) is not int or total < 0):
                raise ValueError
            # Keep provider reasoning fields for protocol compatibility only.
            # They never enter public events, summaries or model output panels.
            return {'message': msg, 'tokens': total}
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderError('provider_invalid_response') from None
