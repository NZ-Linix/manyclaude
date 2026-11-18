import http.client
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from global_gateway import GlobalGateway, RoutingError, route_id


class GatewayTests(unittest.TestCase):
    def test_plain_id_uses_usage_and_pinned_ids_use_only_the_named_account(self):
        model = 'claude-opus-5-5'
        with tempfile.TemporaryDirectory() as directory:
            manager = SimpleNamespace(BASE=Path(directory))
            profiles = {'main': {}, 'work': {}}
            now = time.time()
            usage = {'main': {'identity': 'main', 'windows': {
                'seven_day': {'used_percentage': 100, 'resets_at': now + 300, 'observed_at': now},
            }}, 'work': {'identity': 'work'}}
            with GlobalGateway(manager, profiles, usage, {model: ['main', 'work']}, directory, 'main', []) as gateway:
                self.assertEqual(route_id(model), model)
                self.assertEqual(route_id(model, 'main'), model + '@main')
                self.assertEqual(gateway.resolve(model), (model, ['work'], False))
                self.assertEqual(gateway.resolve(model + '@main'), (model, ['main'], True))
                self.assertEqual(gateway.resolve('manyclaude/' + model), (model, ['work'], False))
                self.assertEqual(gateway.resolve('manyclaude/' + model + '@main'), (model, ['main'], True))
                with self.assertRaises(RoutingError):
                    gateway.resolve(model + '@missing')

    def test_pinned_and_automatic_requests_preserve_session(self):
        model = 'claude-sonnet-5-5'
        with tempfile.TemporaryDirectory() as directory:
            manager = SimpleNamespace(BASE=Path(directory))
            profiles = {'main': {}, 'work': {}}
            usage = {'main': {'identity': 'main'}, 'work': {'identity': 'work'}}
            with GlobalGateway(manager, profiles, usage, {model: ['main', 'work']}, directory, 'main', []) as gateway:
                client = http.client.HTTPConnection('127.0.0.1', gateway.server.server_port, timeout=5)
                self.addCleanup(client.close)
                headers = {'X-ManyClaude': gateway.secret, 'Authorization': 'Bearer ignored',
                           'X-Claude-Code-Session-Id': 'same-session'}
                response = SimpleNamespace(getheaders=lambda: [], status=200,
                                           getheader=lambda *args: 'text/event-stream',
                                           readline=io.BytesIO(b'data: {"type":"message_stop"}\n\n').readline)
                with patch.object(gateway, 'credentials', side_effect=lambda account: (account, account)), \
                        patch.object(gateway, 'observe'), patch('global_gateway.http.client.HTTPSConnection') as upstream:
                    upstream.return_value.getresponse.return_value = response
                    turns = [{'role': 'user', 'content': 'First turn'}]
                    for route, expected in [(route_id(model), 'main'), (route_id(model, 'work'), 'work')]:
                        body = {'model': route, 'messages': turns, 'metadata': {'user_id': '{"account_uuid":"main"}'}}
                        client.request('POST', '/v1/messages?beta=true', json.dumps(body), headers)
                        self.assertEqual(client.getresponse().status, 200)
                        _, path, payload = upstream.return_value.request.call_args.args
                        forwarded = upstream.return_value.request.call_args.kwargs['headers']
                        self.assertEqual(path, '/v1/messages?beta=true')
                        self.assertEqual(forwarded['Authorization'], 'Bearer ' + expected)
                        self.assertEqual(forwarded['X-Claude-Code-Session-Id'], 'same-session')
                        self.assertNotIn('X-ManyClaude', forwarded)
                        request = json.loads(payload)
                        self.assertEqual(request['model'], model)
                        self.assertEqual(request['messages'], turns)
                        self.assertEqual(json.loads(request['metadata']['user_id'])['account_uuid'], expected)
                        client.close()
                        turns.append({'role': 'user', 'content': 'Next turn'})

                client.request('POST', '/v1/messages', '{}')
                self.assertEqual(client.getresponse().status, 401)
