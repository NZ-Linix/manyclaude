"""Route a native Claude Code session's Messages requests to saved profiles."""
from contextlib import suppress
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import subprocess
from threading import Lock, Thread
import time
from urllib.parse import urlsplit

from global_code import LEGACY_ROUTE_PREFIX, digest, eligible_accounts, json_object, read_json, save_usage
from openusage import read_credentials
from profile_config import config_path

UPSTREAM = 'api.anthropic.com'
MAX_REQUEST = 32 * 1024 * 1024
MESSAGES_PATHS = {'/v1/messages', '/v1/messages/count_tokens'}
PRIVATE_HEADERS = {'authorization', 'x-api-key', 'x-manyclaude', 'host', 'connection',
                   'content-length', 'transfer-encoding', 'accept-encoding', 'proxy-authorization',
                   'te', 'trailer', 'upgrade', 'keep-alive', 'expect'}
RESPONSE_HEADERS = {'content-type', 'content-encoding', 'request-id', 'retry-after'}


def route_id(model, account=None):
    return f'{model}@{account}' if account else model


class RoutingError(Exception):
    def __init__(self, status, kind, message):
        super().__init__(message)
        self.status, self.kind = status, kind


class GlobalGateway:
    def __init__(self, manager, profiles, usage, available, workspace, default, rows):
        self.manager, self.profiles, self.usage = manager, profiles, usage
        self.available, self.workspace, self.default = available, workspace, default
        self.rows = rows
        self.routes = {route_id(model, key): (model, key)
                       for model, accounts in available.items() for key in (None, *accounts)}
        # Prompt caches belong to one account. Keep each conversation on the account
        # that holds its cache instead of re-ranking every turn.
        self.sticky = {}
        self.cache_ttl = {}
        self.lock = Lock()
        self.secret = secrets.token_urlsafe(32)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), RequestHandler)
        self.server.gateway = self
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def environment(self):
        return {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.server.server_port}',
                'ANTHROPIC_CUSTOM_HEADERS': f'X-ManyClaude: {self.secret}',
                'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run(self, command, env):
        return subprocess.run(command, env=self.session_environment(env)).returncode

    def session_environment(self, env):
        # Inherit provider OAuth behavior from Claude Code. The router replaces
        # only the authorization on allowlisted inference requests.
        headers = env.get('ANTHROPIC_CUSTOM_HEADERS', '')
        overlay = self.environment
        if headers:
            overlay['ANTHROPIC_CUSTOM_HEADERS'] = headers + '\n' + overlay['ANTHROPIC_CUSTOM_HEADERS']
        for name in ('NO_PROXY', 'no_proxy'):
            if env.get(name):
                overlay[name] = env[name] + ',' + overlay[name]
        return {**env, **overlay}

    def resolve(self, model, session=None):
        route = model.removeprefix(LEGACY_ROUTE_PREFIX)
        if route not in self.routes and route.endswith('[1m]'):
            route = route.removesuffix('[1m]')
        if route not in self.routes and (model.startswith(LEGACY_ROUTE_PREFIX) or '@' in route):
            raise RoutingError(404, 'not_found_error', 'Unknown ManyClaude model. Reopen /model to see available routes.')
        canonical, pinned = self.routes.get(route, (model, None))
        if pinned:
            return canonical, [pinned], True
        if route not in self.routes:
            return canonical, [self.default], True
        accounts = self.available[canonical]
        usage = {key: {**self.usage[key], **read_json(self.manager.BASE / 'global-usage' /
                                                     (self.usage[key]['identity'] + '.json'))} for key in accounts}
        now = time.time()
        ranked = eligible_accounts(accounts, usage, self.workspace, canonical, now)
        if not ranked:
            raise RoutingError(429, 'rate_limit_error', 'All accounts for this model are at a usage limit.')
        if session:
            with self.lock:
                preferred = self.sticky.get((session, canonical))
            if preferred is None:
                # A resumed conversation in a new process: prefer the account whose
                # cache this session warmed most recently.
                key = digest(self.workspace, canonical)
                preferred = next((account for account in ranked
                                  if (entry := usage[account].get('cache', {}).get(key, {})).get('session_id') == session
                                  and entry.get('expires_at', 0) > now), None)
            if preferred in ranked:
                ranked.remove(preferred)
                ranked.insert(0, preferred)
        return canonical, ranked, False

    def remember(self, session, model, account):
        if session:
            with self.lock:
                self.sticky[(session, model)] = account

    def credentials(self, account):
        profile = self.profiles[account]
        # Never copy or renew refresh tokens. Read the live store on demand so
        # Claude Code or OpenUsage can refresh it through their existing flow.
        raw, _ = read_credentials(profile)
        oauth = json_object(raw or '').get('claudeAiOauth') or {}
        token, expiry = oauth.get('accessToken'), oauth.get('expiresAt')
        if not isinstance(token, str) or not token or not isinstance(expiry, (float, int)) or expiry / 1000 <= time.time():
            name = profile['name']
            raise RoutingError(401, 'authentication_error', f'{name} needs a fresh login through its Claude Code command.')
        identity = read_json(config_path(profile)).get('oauthAccount') or {}
        return token, identity.get('accountUuid')

    def observe(self, account, model, headers, message_usage, session):
        windows = {}
        normalized = {key.lower(): value for key, value in headers}
        for abbreviation, name in [('5h', 'five_hour'), ('7d', 'seven_day')]:
            prefix = f'anthropic-ratelimit-unified-{abbreviation}-'
            try:
                percentage = float(normalized[prefix + 'utilization']) * 100
                reset = float(normalized[prefix + 'reset'])
            except (KeyError, ValueError):
                continue
            windows[name] = {'used_percentage': min(100, percentage), 'resets_at': reset}
        details = message_usage.get('cache_creation') or {}
        cached = message_usage.get('cache_read_input_tokens', 0) or message_usage.get('cache_creation_input_tokens', 0)
        # Cache reads refresh an entry for the TTL it was written with; reads report no TTL.
        with self.lock:
            key = (account, model, session)
            if details.get('ephemeral_1h_input_tokens'):
                self.cache_ttl[key] = 3600
            elif details.get('ephemeral_5m_input_tokens'):
                self.cache_ttl.setdefault(key, 300)
            ttl = self.cache_ttl.get(key, 300)
        data = {'rate_limits': windows, 'model': {'id': model}, 'session_id': session,
                'workspace': {'project_dir': self.workspace}}
        if cached:
            data['prompt_cache'] = {'warm': True, 'expires_at': time.time() + ttl}
        with suppress(OSError, ValueError):
            save_usage(self.manager, self.usage[account]['identity'], data)


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def gateway(self):
        return self.server.gateway

    def reply(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authenticate(self):
        secret = self.headers.get('X-ManyClaude', '')
        if not hmac.compare_digest(secret.encode(), self.gateway.secret.encode()):
            raise RoutingError(401, 'authentication_error', 'Invalid local ManyClaude credential.')

    def do_GET(self):
        try:
            self.authenticate()
            if urlsplit(self.path).path != '/v1/models':
                raise RoutingError(404, 'not_found_error', 'This endpoint is unavailable through ManyClaude.')
            models = [{'id': row['model'], 'display_name': row['label'], 'type': 'model',
                       'created_at': '2026-01-01T00:00:00Z'} for row in self.gateway.rows]
            self.reply(200, {'data': models, 'has_more': False})
        except RoutingError as error:
            self.error_reply(error)

    def error_reply(self, error):
        with suppress(OSError):
            self.reply(error.status, {'type': 'error', 'error': {'type': error.kind, 'message': str(error)}})

    def do_POST(self):
        self.connection.settimeout(45)
        try:
            self.authenticate()
            url = urlsplit(self.path)
            if url.path not in MESSAGES_PATHS or url.netloc or url.scheme:
                raise RoutingError(404, 'not_found_error', 'This endpoint is unavailable through ManyClaude.')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_REQUEST:
                raise RoutingError(400, 'invalid_request_error', 'Invalid request length.')
            raw = self.rfile.read(length)
            body = json_object(raw)
            requested = body.get('model')
            if not isinstance(requested, str):
                raise RoutingError(400, 'invalid_request_error', 'Expected a model ID.')
            self.forward(url.path + ('?' + url.query if url.query else ''), raw, body, requested)
        except RoutingError as error:
            self.error_reply(error)
        except (ValueError, TypeError):
            self.error_reply(RoutingError(400, 'invalid_request_error', 'Invalid Messages request.'))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def forward(self, path, raw, body, requested):
        session = self.headers.get('X-Claude-Code-Session-Id')
        model, accounts, strict = self.gateway.resolve(requested, session)
        headers = {key: value for key, value in self.headers.items() if key.lower() not in PRIVATE_HEADERS}
        headers['Accept-Encoding'] = 'identity'
        # Change the top-level model while preserving the original body when its
        # model field comes first, as it does in Claude Code's Messages requests.
        pattern = rb'(\{\s*"model"\s*:\s*)' + re.escape(json.dumps(requested).encode())
        if requested != model:
            body['model'] = model
            rewritten, count = re.subn(pattern, lambda match: match[1] + json.dumps(model).encode(), raw, count=1)
            raw = rewritten if count else json.dumps(body, separators=(',', ':')).encode()
        for index, account in enumerate(accounts):
            another = not strict and index < len(accounts) - 1
            try:
                token, account_uuid = self.gateway.credentials(account)
            except RoutingError:
                if another:
                    continue
                raise
            payload = raw
            metadata = body.get('metadata') or {}
            user = json_object(metadata.get('user_id') or '')
            if user.get('account_uuid') and account_uuid and user['account_uuid'] != account_uuid:
                user['account_uuid'] = account_uuid
                changed_metadata = {**metadata, 'user_id': json.dumps(user, separators=(',', ':'))}
                payload = json.dumps({**body, 'metadata': changed_metadata}, separators=(',', ':')).encode()
            headers['Authorization'] = 'Bearer ' + token
            connection = http.client.HTTPSConnection(UPSTREAM, timeout=120)
            try:
                try:
                    connection.request('POST', path, payload, headers=headers)
                    response = connection.getresponse()
                except (OSError, http.client.HTTPException) as error:
                    raise RoutingError(502, 'api_error', 'Could not reach the Claude Messages API.') from error
                response_headers = response.getheaders()
                if response.status == 429 and another:
                    self.gateway.observe(account, model, response_headers, {}, session)
                    continue
                if response.status < 400 and urlsplit(path).path == '/v1/messages':
                    self.gateway.remember(session, model, account)
                self.send_response(response.status)
                for key, value in response_headers:
                    lower = key.lower()
                    if lower in RESPONSE_HEADERS or (lower.startswith('anthropic-ratelimit-') and strict):
                        self.send_header(key, value)
                self.end_headers()
                cache_usage = {}
                try:
                    if 'text/event-stream' in response.getheader('Content-Type', ''):
                        for line in iter(response.readline, b''):
                            if line.startswith(b'data:'):
                                event = json_object(line[5:])
                                cache_usage.update((event.get('message') or {}).get('usage') or {})
                                cache_usage.update(event.get('usage') or {})
                            self.wfile.write(line)
                    else:
                        content = response.read()
                        cache_usage = json_object(content).get('usage') or {}
                        self.wfile.write(content)
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    self.gateway.observe(account, model, response_headers, cache_usage, session)
                return
            finally:
                connection.close()
