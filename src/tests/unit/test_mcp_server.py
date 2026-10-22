# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DebOps MCP server dispatch.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import rpc
from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer, PROTOCOL_VERSION, RequestCancelled
from debops.mcp.server import _config_signature
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock


def make_request(method, params=None, request_id=1, meta='default'):
    message = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if meta == 'default':
        # The per-request fields the specification requires
        meta = {
            'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
            'io.modelcontextprotocol/clientCapabilities': {},
        }
    # The request metadata rides in 'params._meta', per the spec; a
    # deliberately malformed (non-dict) params passes through untouched
    if meta is not None and (params is None or isinstance(params, dict)):
        params = dict(params or {})
        params['_meta'] = meta
    if params is not None:
        message['params'] = params
    return message


class ResourceTemplatesTestCase(unittest.TestCase):
    """The 2025-era template probe gets an empty answer, not an
    unknown-method error."""

    def test_templates_list_is_empty(self):
        server = McpServer()
        response = server.handle(make_request('resources/templates/'
                                              'list'))
        self.assertNotIn('error', response)
        self.assertEqual(response['result']['resourceTemplates'], [])


class DiscoverTestCase(unittest.TestCase):
    def test_discover(self):
        server = McpServer()
        response = server.handle(make_request('server/discover'))
        self.assertEqual(response['jsonrpc'], '2.0')
        self.assertEqual(response['id'], 1)

        result = response['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertEqual(result['supportedVersions'], [PROTOCOL_VERSION])
        self.assertIn('tools', result['capabilities'])
        # The ordering guarantee (or lack of it) is documented for
        # agents; regressions here silently break write-then-read
        # workflows
        self.assertIn('concurrently', result['instructions'])
        self.assertIn('wait for its response', result['instructions'])
        self.assertEqual(result['ttlMs'], 60000)
        self.assertEqual(result['cacheScope'], 'private')

        meta = result['_meta']['io.modelcontextprotocol/serverInfo']
        self.assertEqual(meta['name'], 'debops')
        self.assertTrue(meta['version'])

    def _discover_instructions(self, **server_kwargs):
        server = McpServer(**server_kwargs)
        response = server.handle(make_request('server/discover'))
        return response['result']['instructions']

    def test_instructions_for_a_project_over_stdio(self):
        project = type('Project', (), {'name': 'containers',
                                       'view': 'system'})()
        text = self._discover_instructions(project=project)
        # The nudge: the tools, not direct file reads
        self.assertIn('Prefer the debops tools', text)
        self.assertIn("'containers'", text)
        self.assertIn("'system'", text)
        self.assertIn('project.read', text)
        self.assertIn('concurrently', text)

    def test_instructions_for_a_daemon_over_a_socket(self):
        project = type('Project', (), {'name': 'containers',
                                       'view': 'system'})()
        text = self._discover_instructions(project=project,
                                           transport='unix')
        # A bridged client may not have the project files at all
        self.assertIn('daemon', text)
        self.assertIn('the access path', text)
        self.assertNotIn('Prefer the debops tools', text)

    def test_instructions_for_the_documentation_mode(self):
        # No project is claimed where there is none
        text = self._discover_instructions(project=None)
        self.assertIn('documentation', text)
        self.assertIn('read-only', text)
        self.assertNotIn('project.files', text)

    def test_discover_result_shape_matches_the_spec(self):
        # The exact key sets of the answer, per the 2026-07-28 schema
        # (DiscoverResult extends CacheableResult): guards against both
        # omitted and invented fields
        server = McpServer()
        result = server.handle(make_request('server/discover'))['result']
        self.assertEqual(sorted(result),
                         ['_meta', 'cacheScope', 'capabilities',
                          'instructions', 'resultType',
                          'supportedVersions', 'ttlMs'])
        # The server identity rides _meta only; the draft layout's body
        # fields never shipped
        for invented in ('protocolVersion', 'serverInfo',
                         'authorization'):
            self.assertNotIn(invented, result)
        self.assertEqual(sorted(result['capabilities']),
                         ['extensions', 'tools'])
        self.assertEqual(
                sorted(result['capabilities']['extensions']),
                ['io.modelcontextprotocol/tasks'])
        self.assertEqual(sorted(result['_meta']),
                         ['io.modelcontextprotocol/serverInfo',
                          'org.debops/clacksOverhead'])

    def test_request_without_meta_is_rejected(self):
        server = McpServer()
        response = server.handle(make_request('server/discover',
                                              meta=None))
        error = response['error']
        self.assertEqual(error['code'], rpc.INVALID_PARAMS)
        self.assertIn('_meta', error['data']['details'])

    def test_request_without_capabilities_is_rejected(self):
        server = McpServer()
        response = server.handle(make_request(
                'server/discover',
                meta={'io.modelcontextprotocol/protocolVersion':
                      PROTOCOL_VERSION}))
        error = response['error']
        self.assertEqual(error['code'], rpc.INVALID_PARAMS)
        self.assertIn('clientCapabilities', error['data']['details'])

    def test_unsupported_protocol_version(self):
        server = McpServer()
        response = server.handle(make_request(
                'server/discover',
                meta={'io.modelcontextprotocol/protocolVersion':
                      '2025-11-25',
                      'io.modelcontextprotocol/clientCapabilities': {}}))
        error = response['error']
        self.assertEqual(error['code'], rpc.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(error['data']['supported'], [PROTOCOL_VERSION])
        self.assertEqual(error['data']['requested'], '2025-11-25')


class EnvelopeTestCase(unittest.TestCase):

    def test_notification_gets_no_response(self):
        server = McpServer()
        request = make_request('server/discover')
        del request['id']
        self.assertIsNone(server.handle(request))

    def test_unknown_method(self):
        server = McpServer()
        response = server.handle(make_request('resources/list'))
        self.assertEqual(response['error']['code'], rpc.METHOD_NOT_FOUND)
        self.assertEqual(response['error']['data'],
                         {'method': 'resources/list'})

    def test_initialize_names_the_supported_versions(self):
        # A legacy client has no fall-forward mechanism; the error is
        # the only diagnostic it gets
        server = McpServer()
        response = server.handle({'jsonrpc': '2.0', 'id': 1,
                                  'method': 'initialize',
                                  'params': {}})
        error = response['error']
        self.assertEqual(error['code'], rpc.METHOD_NOT_FOUND)
        self.assertEqual(error['data']['supported'],
                         [PROTOCOL_VERSION])

    def test_internal_error_names_the_exception(self):
        # The journal keeps the traceback; the message tells the caller
        # what failed — both readers are the same person here
        server = McpServer()

        def broken(arguments):
            raise RuntimeError('a storm of bees')

        server.register_tool('broken', 'Broken', {'type': 'object'},
                             broken)
        response = server.handle(make_request(
                'tools/call', params={'name': 'broken', 'arguments': {}}))
        error = response['error']
        self.assertEqual(error['code'], rpc.INTERNAL_ERROR)
        self.assertIn('RuntimeError', error['message'])
        self.assertIn('a storm of bees', error['message'])

    def test_list_rejects_a_cursor(self):
        # The lists are never paginated, so any cursor is invalid
        server = McpServer()
        server.register_tool('status', 'Status', {'type': 'object'},
                             lambda arguments: {'content': [],
                                                'isError': False})
        response = server.handle(make_request(
                'tools/list', params={'cursor': 'eyJwYWdlIjoyfQ=='}))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('cursor', response['error']['data']['details'])

    def test_unknown_notification_ignored(self):
        server = McpServer()
        request = make_request('notifications/unknown')
        del request['id']
        self.assertIsNone(server.handle(request))

    def test_missing_method(self):
        server = McpServer()
        response = server.handle({'jsonrpc': '2.0', 'id': 1})
        self.assertEqual(response['error']['code'], rpc.INVALID_REQUEST)

    def test_params_must_be_an_object(self):
        server = McpServer()
        response = server.handle(make_request('server/discover',
                                              params=['not', 'a', 'dict']))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)

    def test_string_request_id_preserved(self):
        server = McpServer()
        response = server.handle(make_request('server/discover',
                                              request_id='req-42'))
        self.assertEqual(response['id'], 'req-42')


class HandleBytesTestCase(unittest.TestCase):

    def test_garbage_bytes(self):
        server = McpServer()
        response = server.handle_bytes(b'not json')
        self.assertEqual(response['error']['code'], rpc.PARSE_ERROR)
        self.assertIsNone(response['id'])

    def test_valid_bytes(self):
        server = McpServer()
        response = server.handle_bytes(json.dumps(
                make_request('tools/list', request_id=9)).encode())
        self.assertEqual(response['id'], 9)
        self.assertIn('tools', response['result'])


class ToolsTestCase(unittest.TestCase):

    def test_tools_list_empty(self):
        server = McpServer()
        response = server.handle(make_request('tools/list'))
        result = response['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertEqual(result['tools'], [])
        # CacheableResult: clients get a freshness hint and a scope
        self.assertEqual(result['ttlMs'], 60000)
        self.assertEqual(result['cacheScope'], 'private')

    def test_tools_call_unknown_tool(self):
        server = McpServer()
        response = server.handle(make_request(
                'tools/call', params={'name': 'nope', 'arguments': {}}))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('nope', response['error']['data']['details'])

    def test_alias_resolves_to_the_canonical_tool(self):
        server = McpServer()
        server.register_tool(
                'run', 'Run it', {'type': 'object'},
                lambda arguments: {'content': [{'type': 'text',
                                                'text': 'ran'}],
                                   'isError': False})
        server.aliases['playbook.run'] = 'run'
        response = server.handle(make_request(
                'tools/call',
                params={'name': 'playbook.run', 'arguments': {}}))
        self.assertEqual(response['result']['content'][0]['text'],
                         'ran')

    def test_alias_is_never_listed(self):
        server = McpServer()
        server.register_tool(
                'run', 'Run it', {'type': 'object'},
                lambda arguments: {'content': [], 'isError': False})
        server.aliases['playbook.run'] = 'run'
        response = server.handle(make_request('tools/list'))
        names = [tool['name'] for tool in response['result']['tools']]
        self.assertEqual(names, ['run'])

    def test_alias_without_the_canonical_is_an_unknown_tool(self):
        # A dangling alias (the canonical tool is gated off) resolves
        # to nothing
        server = McpServer()
        server.aliases['playbook.run'] = 'run'
        response = server.handle(make_request(
                'tools/call',
                params={'name': 'playbook.run', 'arguments': {}}))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('unknown tool: playbook.run',
                      response['error']['data']['details'])

    def test_an_unknown_tool_gets_a_close_match(self):
        # A near-miss name gets the pointer instead of a retry loop
        server = McpServer()
        server.register_tool('playbook.list', 'List', {'type': 'object'},
                             lambda arguments: {})
        server.register_tool('playbook.info', 'Info', {'type': 'object'},
                             lambda arguments: {})
        response = server.handle(make_request(
                'tools/call',
                params={'name': 'playbook.lst', 'arguments': {}}))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        details = response['error']['data']['details']
        self.assertIn('unknown tool: playbook.lst', details)
        self.assertIn("'playbook.list'", details)

    def test_a_normalized_name_is_matched_through_the_underscore(self):
        # Harnesses which show the model underscore-normalized names
        # get near-misses like 'playbook_list' back
        server = McpServer()
        server.register_tool('playbook.list', 'List', {'type': 'object'},
                             lambda arguments: {})
        response = server.handle(make_request(
                'tools/call',
                params={'name': 'playbook_list', 'arguments': {}}))
        self.assertIn("'playbook.list'",
                      response['error']['data']['details'])

    def test_an_unknown_tool_without_a_match_stays_plain(self):
        server = McpServer()
        server.register_tool('playbook.list', 'List', {'type': 'object'},
                             lambda arguments: {})
        response = server.handle(make_request(
                'tools/call',
                params={'name': 'zzz-no-such', 'arguments': {}}))
        self.assertEqual(response['error']['data']['details'],
                         'unknown tool: zzz-no-such')

    def test_tool_calls_are_audited(self):
        server = McpServer()
        server.register_tool(
                'write', 'Write', {'type': 'object'},
                lambda arguments: {'content': [{'type': 'text',
                                                'text': 'wrote'}],
                                   'isError': False})
        with self.assertLogs('debops.mcp.server', level='NOTICE') as logs:
            server.handle(make_request(
                    'tools/call',
                    params={'name': 'write',
                            'arguments': {'content': 'SECRET-CANARY'}}))
        (line,) = [line for line in logs.output if 'Tool write(' in line]
        # The keys are logged, the values never
        self.assertIn('content', line)
        self.assertNotIn('SECRET-CANARY', line)
        self.assertIn('-> ok', line)

    def test_the_audit_line_marks_errors(self):
        server = McpServer()
        server.register_tool(
                'broken', 'Broken', {'type': 'object'},
                lambda arguments: {'content': [{'type': 'text',
                                                'text': 'nope'}],
                                   'isError': True})
        with self.assertLogs('debops.mcp.server', level='NOTICE') as logs:
            server.handle(make_request(
                    'tools/call', params={'name': 'broken',
                                          'arguments': {}}))
        (line,) = [line for line in logs.output if 'Tool broken(' in line]
        self.assertIn('-> error', line)

    def test_the_audit_line_names_the_client(self):
        # The durable record of who called: the client identifies
        # itself in the request '_meta' envelope
        server = McpServer()
        server.register_tool(
                'probe', 'Probe', {'type': 'object'},
                lambda arguments: {'content': [{'type': 'text',
                                                'text': 'ok'}],
                                   'isError': False})
        meta = {
            'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
            'io.modelcontextprotocol/clientCapabilities': {},
            'io.modelcontextprotocol/clientInfo': {
                'name': 'testharness', 'version': '1.2.3'},
        }
        with self.assertLogs('debops.mcp.server', level='NOTICE') as logs:
            server.handle(make_request(
                    'tools/call',
                    params={'name': 'probe', 'arguments': {}},
                    meta=meta))
        (line,) = [line for line in logs.output if 'Tool probe(' in line]
        self.assertIn('-> ok [testharness 1.2.3]', line)
        # No identity, no tag
        with self.assertLogs('debops.mcp.server', level='NOTICE') as logs:
            server.handle(make_request(
                    'tools/call',
                    params={'name': 'probe', 'arguments': {}}))
        (line,) = [line for line in logs.output if 'Tool probe(' in line]
        self.assertNotIn('[', line)

    def test_registered_tool_listed_and_called(self):
        server = McpServer()

        def echo(arguments):
            return {'content': [{'type': 'text',
                                 'text': arguments.get('text', '')}],
                    'isError': False}

        server.register_tool(
                'echo', 'Echo the given text',
                {'type': 'object',
                 'properties': {'text': {'type': 'string'}}},
                echo)

        response = server.handle(make_request('tools/list'))
        tools = response['result']['tools']
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['name'], 'echo')
        self.assertEqual(tools[0]['description'], 'Echo the given text')
        self.assertNotIn('outputSchema', tools[0])
        self.assertNotIn('annotations', tools[0])

        response = server.handle(make_request(
                'tools/call',
                params={'name': 'echo', 'arguments': {'text': 'hello'}}))
        result = response['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'],
                         [{'type': 'text', 'text': 'hello'}])

    def test_tool_meta_listed(self):
        server = McpServer()
        server.register_tool(
                'gated', 'Gated thing', {'type': 'object'},
                lambda arguments: {'content': [], 'isError': False},
                meta={'org.debops/confirm': True})

        response = server.handle(make_request('tools/list'))
        (tool,) = response['result']['tools']
        self.assertEqual(tool['_meta'], {'org.debops/confirm': True})

    def test_tool_annotations_listed(self):
        server = McpServer()
        server.register_tool(
                'wipe', 'Wipe things', {'type': 'object'},
                lambda arguments: {'content': [], 'isError': False},
                annotations={'destructiveHint': True,
                             'idempotentHint': False})

        response = server.handle(make_request('tools/list'))
        (tool,) = response['result']['tools']
        self.assertEqual(tool['annotations'],
                         {'destructiveHint': True,
                          'idempotentHint': False})

    def test_tools_call_arguments_must_be_an_object(self):
        server = McpServer()
        server.register_tool('echo', 'Echo', {'type': 'object'},
                             lambda arguments: {'content': []})
        response = server.handle(make_request(
                'tools/call', params={'name': 'echo', 'arguments': ['x']}))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)


class CancellationTestCase(unittest.TestCase):

    def test_cancellation_stops_and_suppresses_the_request(self):
        server = McpServer()
        started = threading.Event()

        def slow(arguments, context):
            started.set()
            context.cancelled.wait(10)
            raise RequestCancelled()

        server.register_tool('slow', 'A slow tool', {'type': 'object'},
                             slow, needs_context=True)

        outcome = []

        def call():
            outcome.append(server.handle(make_request(
                    'tools/call', params={'name': 'slow',
                                          'arguments': {}})))

        thread = threading.Thread(target=call)
        thread.start()
        self.assertTrue(started.wait(5))

        # The cancellation notification itself gets no response
        self.assertIsNone(server.handle({
                'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                'params': {'requestId': 1}}))
        thread.join(timeout=5)
        # ... and neither does the cancelled request
        self.assertEqual(outcome, [None])

    def test_peer_pid_reaches_the_tool_context(self):
        server = McpServer()
        seen = []

        def probe(arguments, context):
            seen.append(context.peer_pid)
            return {'content': [], 'isError': False}

        server.register_tool('probe', 'Probe', {'type': 'object'},
                             probe, needs_context=True)
        server.handle(make_request('tools/call',
                                   params={'name': 'probe',
                                           'arguments': {}}),
                      peer_pid=4321)
        self.assertEqual(seen, [4321])

    def test_cancellation_of_unknown_request_is_ignored(self):
        server = McpServer()
        self.assertIsNone(server.handle({
                'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                'params': {'requestId': 'nosuch'}}))

    def test_tool_without_context_needs_no_registry(self):
        server = McpServer()
        server.register_tool('plain', 'Plain tool', {'type': 'object'},
                             lambda arguments: {'content': [],
                                                'isError': False})
        response = server.handle(make_request(
                'tools/call', params={'name': 'plain', 'arguments': {}}))
        self.assertEqual(response['result']['content'], [])
        self.assertEqual(server._active_requests, {})


class ProgressTestCase(unittest.TestCase):
    """Progress notifications travel the transport's notify channel."""

    def _noisy_server(self):
        server = McpServer()

        def noisy(arguments, context):
            context.progress('halfway', progress=1)
            return {'content': []}

        server.register_tool('noisy', 'Noisy tool', {'type': 'object'},
                             noisy, needs_context=True)
        return server

    def test_progress_with_a_token(self):
        server = self._noisy_server()
        sent = []
        request = make_request('tools/call', params={'name': 'noisy',
                                                     'arguments': {}},
                               meta={'progressToken': 'pt-42',
                                     'io.modelcontextprotocol/'
                                     'protocolVersion': PROTOCOL_VERSION,
                                     'io.modelcontextprotocol/'
                                     'clientCapabilities': {}})
        response = server.handle_bytes(json.dumps(request).encode(),
                                       notify=sent.append)
        self.assertIsNotNone(response)
        notifications = [message for message in sent
                         if message.get('method')
                         == 'notifications/progress']
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]['params']['progressToken'],
                         'pt-42')
        self.assertEqual(notifications[0]['params']['message'], 'halfway')

    def test_no_token_no_progress(self):
        server = self._noisy_server()
        sent = []
        request = make_request('tools/call', params={'name': 'noisy',
                                                     'arguments': {}})
        response = server.handle_bytes(json.dumps(request).encode(),
                                       notify=sent.append)
        self.assertIsNotNone(response)
        self.assertEqual(sent, [])


class ConfirmationGateTestCase(unittest.TestCase):
    """Tools named in the policy's confirm list ask the user first."""

    def _server(self, confirm=('run',), confirm_required=False):
        policy = type('Policy', (), {
                'confirm': set(confirm),
                'confirm_required': confirm_required})()
        server = McpServer(policy=policy)
        self.ran = []
        server.register_tool(
                'run', 'Run things', {'type': 'object'},
                lambda arguments: self.ran.append(arguments) or {
                    'content': [{'type': 'text', 'text': 'ran'}],
                    'isError': False})
        return server

    def _call(self, server, capabilities=None, input_responses=None):
        params = {'name': 'run', 'arguments': {'playbook': 'site'}}
        if input_responses is not None:
            params['inputResponses'] = input_responses
        meta = {'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
                'io.modelcontextprotocol/clientCapabilities':
                capabilities if capabilities is not None else {}}
        return server.handle(make_request('tools/call', params=params,
                                          meta=meta))

    def test_gated_tool_elicits_confirmation(self):
        server = self._server()
        response = self._call(server,
                              capabilities={'elicitation': {'form': {}}})
        result = response['result']
        self.assertEqual(result['resultType'], 'input_required')
        request = result['inputRequests']['confirmation']
        self.assertEqual(request['method'], 'elicitation/create')
        self.assertEqual(request['params']['mode'], 'form')
        self.assertIn('site', request['params']['message'])
        # The tool did not run yet
        self.assertEqual(self.ran, [])

    def test_confirmed_call_proceeds(self):
        server = self._server()
        response = self._call(server, input_responses={
                'confirmation': {'action': 'accept',
                                 'content': {'confirmed': True}}})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(self.ran, [{'playbook': 'site'}])

    def test_declined_call_errors(self):
        server = self._server()
        response = self._call(server, input_responses={
                'confirmation': {'action': 'decline'}})
        result = response['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertTrue(result['isError'])
        self.assertIn('declined', result['content'][0]['text'])
        self.assertEqual(self.ran, [])

    def test_accept_without_confirmation_errors(self):
        server = self._server()
        response = self._call(server, input_responses={
                'confirmation': {'action': 'accept',
                                 'content': {'confirmed': False}}})
        self.assertTrue(response['result']['isError'])
        self.assertEqual(self.ran, [])

    def test_no_elicitation_capability_proceeds(self):
        server = self._server()
        response = self._call(server, capabilities={})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(self.ran, [{'playbook': 'site'}])

    def test_confirm_required_refuses_a_client_which_cannot_elicit(
            self):
        server = self._server(confirm_required=True)
        response = self._call(server, capabilities={})
        result = response['result']
        self.assertTrue(result['isError'])
        self.assertIn('confirm_required', result['content'][0]['text'])
        self.assertEqual(self.ran, [])

    def test_confirm_required_still_accepts_answers(self):
        server = self._server(confirm_required=True)
        response = self._call(server, input_responses={
                'confirmation': {'action': 'accept',
                                 'content': {'confirmed': True}}})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(self.ran, [{'playbook': 'site'}])

    def test_url_only_capability_proceeds(self):
        # URL-mode elicitation cannot carry the confirmation form
        server = self._server()
        response = self._call(server,
                              capabilities={'elicitation': {'url': {}}})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(self.ran, [{'playbook': 'site'}])

    def test_ungated_tool_proceeds(self):
        server = self._server(confirm=('other',))
        response = self._call(server,
                              capabilities={'elicitation': {'form': {}}})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(self.ran, [{'playbook': 'site'}])

    def test_no_policy_no_gate(self):
        server = McpServer()
        server.register_tool(
                'run', 'Run things', {'type': 'object'},
                lambda arguments: {'content': [], 'isError': False})
        response = server.handle(make_request(
                'tools/call',
                params={'name': 'run', 'arguments': {}}))
        self.assertFalse(response['result']['isError'])

    def test_self_confirming_tool_skips_the_gate(self):
        # The tool runs its own elicitation mid-call, so the generic
        # gate lets it through
        server = self._server()
        server.tools['run'].self_confirms = True
        response = self._call(server,
                              capabilities={'elicitation': {'form': {}}})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(self.ran, [{'playbook': 'site'}])


class ReloadTestCase(unittest.TestCase):
    """The in-daemon policy reload after a pull."""

    def _server(self):
        server = McpServer()
        server.register_tool('alpha', 'A', {'type': 'object'},
                             lambda arguments: {'content': [],
                                                'isError': False})
        return server

    def test_reload_rebuilds_and_announces(self):
        server = self._server()

        def rebuild():
            server.tools = {}
            server.register_tool('beta', 'B', {'type': 'object'},
                                 lambda arguments: {'content': [],
                                                    'isError': False})

        server._rebuild = rebuild
        with mock.patch.object(server, 'emit_list_changed') as emit:
            server.reload()
        self.assertEqual(sorted(server.tools), ['beta'])
        emit.assert_called_once_with('tools')

    def test_reload_unchanged_announces_nothing(self):
        server = self._server()
        server._rebuild = lambda: None
        with mock.patch.object(server, 'emit_list_changed') as emit:
            self.assertTrue(server.reload())
        emit.assert_not_called()

    def test_reload_without_a_recipe_is_a_noop(self):
        server = self._server()
        self.assertFalse(server.reload())
        self.assertEqual(sorted(server.tools), ['alpha'])

    def test_a_failing_rebuild_keeps_the_current_surface(self):
        # A broken configuration file must not break the daemon
        server = self._server()

        def rebuild():
            server.tools = {}
            raise ValueError('bad TOML')

        server._rebuild = rebuild
        with self.assertLogs('debops.mcp.server', level='WARNING'):
            self.assertFalse(server.reload())
        # Whatever the rebuild did before it failed is visible; the
        # server keeps dispatching
        self.assertNotIn('alpha', server.tools)

        def good_rebuild():
            server.tools = {}
            server.register_tool('alpha', 'A', {'type': 'object'},
                                 lambda arguments: {'content': [],
                                                    'isError': False})

        server._rebuild = good_rebuild
        self.assertTrue(server.reload())
        self.assertEqual(sorted(server.tools), ['alpha'])


class ConfigWatchTestCase(unittest.TestCase):
    """The configuration watcher reloads the policy on changed files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-watch-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.conf_d = os.path.join(self.tmpdir, '.debops', 'conf.d')
        os.makedirs(self.conf_d)
        self.project = type('Project', (), {
            'path': self.tmpdir,
            'name': 'project',
            'project_type': 'legacy',
            'view': 'system',
            'auto_commit': lambda self, message=None: None,
            'config': type('Config', (), {'raw': {},
                                          '_env_vars': {}})(),
        })()
        self.reloads = []
        self.server = McpServer(project=self.project,
                                policy=McpPolicy(self.project))
        self.server._rebuild = lambda: self.reloads.append(
                time.monotonic())
        self.addCleanup(self.server.stop_config_watch)

    def _write_config(self, name='mcp.toml', text=''):
        with open(os.path.join(self.conf_d, name), 'w') as fp:
            fp.write(text)

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_signature_tracks_the_config_files(self):
        # No files yet: the directory listing is empty, no legacy file
        signature = _config_signature(self.project)
        self.assertEqual(signature, ((), None))

        self._write_config(text='[project.mcp]\n')
        changed = _config_signature(self.project)
        self.assertNotEqual(changed, signature)
        signature = changed

        # An edit with an explicit new mtime changes the fingerprint
        path = os.path.join(self.conf_d, 'mcp.toml')
        os.utime(path, ns=(1 << 40, 1 << 40))
        changed = _config_signature(self.project)
        self.assertNotEqual(changed, signature)
        signature = changed

        # An added file, a removed file and a legacy file all change
        # the fingerprint, whatever the format
        self._write_config(name='extra.yml', text='--- {}\n')
        self.assertNotEqual(_config_signature(self.project), signature)
        os.unlink(os.path.join(self.conf_d, 'extra.yml'))
        self.assertEqual(_config_signature(self.project), signature)
        with open(os.path.join(self.tmpdir, '.debops.cfg'), 'w') as fp:
            fp.write('[debops]\n')
        self.assertNotEqual(_config_signature(self.project), signature)

    def watch_with_observed_baseline(self, interval=0.05):
        """Start the watcher; return once it took its baseline, so a
        later change is guaranteed to be noticed."""
        baseline_taken = threading.Event()
        real_signature = _config_signature

        def recording_signature(project):
            # Set the event only once the baseline is computed: setting
            # it on entry would let a change race the read itself
            signature = real_signature(project)
            baseline_taken.set()
            return signature

        with mock.patch('debops.mcp.server._config_signature',
                        recording_signature):
            self.server.watch_config(interval=interval)
            self.assertTrue(baseline_taken.wait(5.0))

    def test_watcher_reloads_on_a_change(self):
        self.watch_with_observed_baseline()
        self._write_config(text='[project.mcp]\n')
        self.assertTrue(self.wait_for(lambda: len(self.reloads) >= 1))

    def test_watcher_ignores_unrelated_files(self):
        self.watch_with_observed_baseline()
        with open(os.path.join(self.tmpdir, 'notes.txt'), 'w') as fp:
            fp.write('not a configuration file\n')
        self.assertFalse(self.wait_for(lambda: self.reloads,
                                       timeout=0.3))

    def test_watcher_survives_a_failing_reload(self):
        attempts = []

        def rebuild():
            attempts.append(1)
            if len(attempts) == 1:
                raise ValueError('broken TOML')

        self.server._rebuild = rebuild
        self.watch_with_observed_baseline()
        self._write_config(text='broken = [')
        self.assertTrue(self.wait_for(lambda: len(attempts) >= 1))
        # The signature advanced past the broken state; the next change
        # is another chance
        self._write_config(text='[project.mcp]\n')
        self.assertTrue(self.wait_for(lambda: len(attempts) >= 2))

    def test_watch_requires_a_reload_recipe(self):
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        server.watch_config(interval=0.05)
        self.assertIsNone(server._config_watch)

    def test_watch_is_idempotent_and_stops(self):
        self.server.watch_config(interval=0.05)
        first = self.server._config_watch
        self.server.watch_config(interval=0.05)
        self.assertIs(self.server._config_watch, first)
        self.server.stop_config_watch()
        self.assertIsNone(self.server._config_watch)
        self.assertTrue(first.is_set())
        count = len(self.reloads)
        self._write_config(text='[project.mcp]\n')
        time.sleep(0.2)
        self.assertEqual(len(self.reloads), count)


class ClacksOverheadTestCase(unittest.TestCase):
    """Every result carries the tribute in its '_meta' overhead."""

    def test_results_carry_the_clacks_overhead(self):
        server = McpServer()
        response = server.handle(make_request('server/discover'))
        meta = response['result']['_meta']
        self.assertEqual(meta['org.debops/clacksOverhead'],
                         'GNU Terry Pratchett')
        self.assertIn('io.modelcontextprotocol/serverInfo', meta)

    def test_error_responses_have_no_meta_slot(self):
        server = McpServer()
        response = server.handle(make_request('nosuch/method'))
        self.assertNotIn('_meta', response)
        self.assertNotIn('_meta', response['error'])

    def test_project_and_view_in_meta_when_present(self):
        project = type('Project', (), {'name': 'containers',
                                       'view': 'system'})()
        server = McpServer(project=project)
        response = server.handle(make_request('server/discover'))
        self.assertEqual(response['result']['_meta']['org.debops/project'],
                         'containers')
        self.assertEqual(response['result']['_meta']['org.debops/view'],
                         'system')

    def test_no_project_no_project_meta(self):
        server = McpServer()
        response = server.handle(make_request('server/discover'))
        self.assertNotIn('org.debops/project', response['result']['_meta'])
        self.assertNotIn('org.debops/view', response['result']['_meta'])


class LegacyModeTestCase(unittest.TestCase):
    """The 2025-era protocol revision, answered with --legacy."""

    def _server(self):
        server = McpServer(legacy=True)
        server.register_tool(
                'probe', 'A probe tool', {'type': 'object'},
                lambda arguments: {'content': [{'type': 'text',
                                                'text': 'ok'}],
                                   'isError': False})
        return server

    def _initialize(self, server, version='2025-11-25', peer_pid=4321):
        return server.handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': version,
                       'capabilities': {},
                       'clientInfo': {'name': 'pi', 'version': '1.3.0'}}},
            peer_pid=peer_pid)

    def test_initialize_handshake(self):
        server = self._server()
        response = self._initialize(server)
        result = response['result']
        self.assertEqual(result['protocolVersion'], '2025-11-25')
        self.assertEqual(result['serverInfo']['name'], 'debops')
        self.assertIn('capabilities', result)
        self.assertIn('instructions', result)
        # The legacy result shape: no resultType, no server stamp
        self.assertNotIn('resultType', result)
        self.assertNotIn('_meta', result)

    def test_an_unknown_legacy_version_falls_to_the_newest(self):
        server = self._server()
        response = self._initialize(server, version='1999-01-01')
        self.assertEqual(response['result']['protocolVersion'],
                         '2025-11-25')

    def test_requests_need_no_meta_envelope(self):
        server = self._server()
        self._initialize(server)
        response = server.handle(
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                 'params': {'name': 'probe', 'arguments': {}}},
                peer_pid=4321)
        result = response['result']
        self.assertEqual(result['content'][0]['text'], 'ok')
        self.assertNotIn('resultType', result)
        self.assertNotIn('_meta', result)

    def test_tools_list_carries_the_legacy_shape(self):
        server = self._server()
        self._initialize(server)
        response = server.handle(
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list',
                 'params': {}}, peer_pid=4321)
        result = response['result']
        self.assertEqual([tool['name'] for tool in result['tools']],
                         ['probe'])
        self.assertNotIn('ttlMs', result)
        self.assertNotIn('cacheScope', result)

    def test_the_handshake_identity_reaches_the_audit_line(self):
        server = self._server()
        self._initialize(server)
        with self.assertLogs('debops.mcp.server', level='NOTICE') as logs:
            server.handle(
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                     'params': {'name': 'probe', 'arguments': {}}},
                    peer_pid=4321)
        (line,) = [line for line in logs.output if 'Tool probe(' in line]
        self.assertIn('-> ok [pi 1.3.0]', line)

    def test_ping(self):
        server = self._server()
        response = server.handle({'jsonrpc': '2.0', 'id': 5,
                                  'method': 'ping', 'params': {}})
        self.assertEqual(response['result'], {})

    def test_non_legacy_mode_rejects_initialize_with_the_hint(self):
        server = McpServer()
        response = server.handle(make_request(
                'initialize',
                params={'protocolVersion': '2025-11-25'}))
        self.assertEqual(response['error']['code'],
                         rpc.METHOD_NOT_FOUND)
        self.assertIn('--legacy', response['error']['message'])

    def test_a_legacy_initialize_is_stateless_per_client(self):
        # Two peers get independent handshakes; the audit identity
        # does not leak across them
        server = self._server()
        self._initialize(server, peer_pid=4321)
        self._initialize(server, peer_pid=9999)
        response = server.handle(
                {'jsonrpc': '2.0', 'id': 3, 'method': 'initialize',
                 'params': {'protocolVersion': '2025-06-18',
                            'capabilities': {},
                            'clientInfo': {'name': 'codex'}}},
                peer_pid=9999)
        self.assertEqual(response['result']['protocolVersion'],
                         '2025-06-18')
        self.assertEqual(
                server._legacy_clients[4321]['clientInfo']['name'],
                'pi')
        self.assertEqual(
                server._legacy_clients[9999]['clientInfo']['name'],
                'codex')


if __name__ == '__main__':
    unittest.main()
