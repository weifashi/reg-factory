"""Sub2API bounded admin connectivity contract; synthetic fixtures only."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'fixture-password-do-not-print'
ACCESS = 'fixture-access-do-not-print'
REFRESH = 'fixture-refresh-do-not-print'
PATHS = ['/health', '/api/v1/auth/login', '/api/v1/admin/compliance',
         '/api/v1/admin/groups/all', '/api/v1/auth/logout']
GROUP = {'id': 7, 'name': 'fixture-gemini', 'platform': 'gemini', 'status': 'active'}


def envelope(data):
    return {'code': 0, 'data': data}


class ProbeAvailabilityTests(unittest.TestCase):
    def test_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.sub2api_probe'))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.subject = importlib.import_module('onboarding.sub2api_probe')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = self.directory / 'admin-access.json'
        self.config.write_text(json.dumps({'url': 'https://fixture.invalid',
                                          'email': 'fixture@example.invalid', 'password': SECRET}))
        self.config.chmod(0o600)
        self.seen = []
        self.responses = {
            PATHS[0]: {'status': 'ok'},
            PATHS[1]: envelope({'access_token': ACCESS, 'refresh_token': REFRESH}),
            PATHS[2]: envelope({'required': False, 'version': 'fixture-v1'}),
            PATHS[3]: envelope([GROUP]),
            PATHS[4]: envelope(None),
        }

    def handler(self, request):
        self.seen.append(request)
        response = self.responses[request.url.path]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            if response.is_stream_consumed:
                return httpx.Response(response.status_code, headers=response.headers,
                                      stream=httpx.ByteStream(response.content))
            return response
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(response).encode()),
                              headers={'content-type': 'application/json'})

    def run_probe(self, **kwargs):
        return self.subject.run_probe(str(self.config), 'fixture-gemini',
                                      transport=httpx.MockTransport(self.handler), **kwargs)

    def assert_safe(self, result):
        output = json.dumps(result)
        for secret in (SECRET, ACCESS, REFRESH, 'fixture.invalid', 'fixture@example.invalid'):
            self.assertNotIn(secret, output)
        self.assertFalse(result['execution_connected'])
        self.assertLessEqual(set(result), {'reason_code', 'health', 'auth', 'compliance',
                                         'group', 'group_id', 'logout', 'execution_connected'})

    def reject_config(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            result = self.run_probe()
        self.assertEqual(result['reason_code'], 'INVALID_CONFIG')
        self.assertEqual(self.seen, [])
        self.assert_safe(result)

    def rewrite(self, **changes):
        value = {'url': 'https://fixture.invalid', 'email': 'fixture@example.invalid', 'password': SECRET}
        value.update(changes)
        self.config.write_text(json.dumps(value))

    def test_success_order_and_payload(self):
        result = self.run_probe()
        self.assertEqual(result['reason_code'], 'READY')
        self.assertEqual(result['group_id'], 7)
        self.assertEqual(result['logout'], 'passed')
        self.assertEqual([r.url.path for r in self.seen], PATHS)
        self.assertEqual([r.method for r in self.seen], ['GET', 'POST', 'GET', 'GET', 'POST'])
        self.assertEqual(json.loads(self.seen[1].content), {'email': 'fixture@example.invalid', 'password': SECRET})
        self.assertEqual(json.loads(self.seen[-1].content), {'refresh_token': REFRESH})
        self.assertEqual(self.seen[2].headers['authorization'], 'Bearer ' + ACCESS)
        self.assert_safe(result)

    def test_config_secret_repr_and_password_not_stripped(self):
        self.rewrite(password='  ' + SECRET + '  ')
        config = self.subject.load_config(str(self.config))
        for value in (SECRET, 'fixture.invalid', 'fixture@example.invalid'):
            self.assertNotIn(value, repr(config))
        self.assertEqual(self.run_probe()['reason_code'], 'READY')
        self.assertEqual(json.loads(self.seen[1].content)['password'], '  ' + SECRET + '  ')

    def test_invalid_config_json_types_controls_and_size(self):
        cases = [b'\xff', b'[]', b'null', b'{', b' ' * (16384 + 1),
                 b'{"url":"https://fixture.invalid","email":"a","password":"x","password":"y"}']
        for content in cases:
            with self.subTest(content_type=type(content).__name__, size=len(content)):
                self.config.write_bytes(content)
                self.reject_config()
        for field in ('url', 'email', 'password'):
            for value in (None, False, 123, [], {}, '', 'x\nx', 'x\x7fx', 'x\u0085x', '\ud800'):
                with self.subTest(field=field, kind=type(value).__name__):
                    self.rewrite(**{field: value})
                    self.reject_config()
        self.rewrite(extra='unexpected')
        self.reject_config()
        self.rewrite(email='   ')
        self.reject_config()

    def test_url_boundaries_reject_before_network(self):
        for url in ('http://fixture.invalid', 'http://127.0.0.1:8000',
                    'ftp://fixture.invalid', 'https://u:p@fixture.invalid',
                    'https://fixture.invalid/a', 'https://fixture.invalid/?q=x',
                    'https://fixture.invalid/#x', 'https://fixture.invalid?',
                    'https://fixture.invalid#', ' https://fixture.invalid',
                    'https://fixture.invalid:bad', 'https://fixture.invalid:0',
                    'https://fixture.invalid\\evil', 'https://', 'https://fixture.invalid/%2f'):
            with self.subTest(url=url):
                self.rewrite(url=url)
                self.reject_config()
        for url in ('http://localhost:8000', 'http://127.0.0.2:8000', 'http://[::1]:8000',
                    'http://127.0.0.1', 'http://127.0.0.1:0'):
            self.rewrite(url=url)
            self.assertEqual(self.run_probe(allow_loopback_http=True)['reason_code'], 'INVALID_CONFIG')
        self.assertEqual(self.seen, [])

    def test_file_and_parent_modes(self):
        for mode in (0o644, 0o400, 0o700, 0o1600):
            self.config.chmod(mode)
            self.reject_config()
        self.config.chmod(0o600)
        for mode in (0o755, 0o750, 0o1700):
            self.directory.chmod(mode)
            self.reject_config()
        self.directory.chmod(0o700)
        actual = os.fstat
        def foreign_owner(fd):
            value = actual(fd)
            fields = list(value)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        with patch.object(self.subject.os, 'fstat', side_effect=foreign_owner):
            self.reject_config()

    def test_symlinks_nonregular_missing_and_parent_jump(self):
        original = self.directory / 'original.json'
        self.config.rename(original)
        self.config.symlink_to(original)
        self.reject_config()
        self.config.unlink()
        self.config.mkdir(mode=0o600)
        self.reject_config()
        self.config.rmdir()
        os.mkfifo(self.config, mode=0o600)
        self.reject_config()
        self.config.unlink()
        self.reject_config()
        original.rename(self.config)
        with tempfile.TemporaryDirectory() as other:
            link = Path(other) / 'linked'
            link.symlink_to(self.directory, target_is_directory=True)
            self.config = link / 'admin-access.json'
            self.reject_config()

    def test_health_must_be_ok(self):
        for value in ({'status': 'bad'}, {'status': True}, [], None):
            self.responses[PATHS[0]] = value
            result = self.run_probe()
            self.assertEqual(result['reason_code'], 'HEALTH_FAILED')
            self.assertEqual(result['logout'], 'not_required')
        self.assertEqual([r.url.path for r in self.seen], [PATHS[0]] * 4)

    def test_http_and_response_failures_are_redacted(self):
        errors = [httpx.Response(401, text=SECRET), httpx.Response(423, text=SECRET),
                  httpx.Response(302, headers={'location': 'https://leak.invalid/' + SECRET}),
                  httpx.Response(200, text='<html>' + SECRET),
                  httpx.Response(200, content=b'{', headers={'content-type': 'application/json'}),
                  httpx.Response(200, content=b'x' * (1024 * 1024 + 1), headers={'content-type': 'application/json'}),
                  httpx.ReadTimeout(SECRET)]
        for error in errors:
            with self.subTest(kind=type(error).__name__):
                self.seen.clear()
                self.responses[PATHS[1]] = error
                result = self.run_probe()
                self.assertEqual(result['reason_code'], 'AUTH_FAILED')
                self.assertEqual([r.url.path for r in self.seen], PATHS[:2])
                self.assert_safe(result)

    def test_auth_strict_schema_and_boolean_code(self):
        for value in ({'code': False, 'data': {'access_token': ACCESS, 'refresh_token': REFRESH}},
                      {'code': 0.0, 'data': {}}, envelope({'access_token': '', 'refresh_token': REFRESH}),
                      envelope({'access_token': ACCESS, 'refresh_token': False}), envelope([])):
            self.responses[PATHS[1]] = value
            self.assertEqual(self.run_probe()['reason_code'], 'AUTH_FAILED')

    def test_compliance_required_stops_groups_and_logs_out(self):
        self.responses[PATHS[2]] = envelope({'required': True, 'version': 'fixture-v1'})
        result = self.run_probe()
        self.assertEqual(result['reason_code'], 'COMPLIANCE_REQUIRED')
        self.assertEqual(result['logout'], 'passed')
        self.assertEqual([r.url.path for r in self.seen], PATHS[:3] + [PATHS[-1]])

    def test_compliance_strict_schema(self):
        for value in ({'required': 0, 'version': 'v'}, {'required': False, 'version': ''},
                      {'required': False, 'version': False}, {'version': 'v'}, []):
            self.seen.clear()
            self.responses[PATHS[2]] = envelope(value)
            result = self.run_probe()
            self.assertEqual(result['reason_code'], 'COMPLIANCE_FAILED')
            self.assertEqual(result['logout'], 'passed')
            self.assertNotIn(PATHS[3], [r.url.path for r in self.seen])

    def test_groups_fail_closed(self):
        for groups in ([], [dict(GROUP, name='other')], [dict(GROUP, status='inactive')],
                       [GROUP, GROUP], [dict(GROUP, platform='openai')],
                       [GROUP, dict(GROUP, platform='openai')], [dict(GROUP, id=True)],
                       [dict(GROUP, id=0)], [dict(GROUP, id=-1)], [dict(GROUP, id='7')], {}, [None]):
            self.responses[PATHS[3]] = envelope(groups)
            result = self.run_probe()
            self.assertEqual(result['reason_code'], 'GROUP_FAILED')
            self.assertEqual(result['logout'], 'passed')
            self.assertNotIn('group_id', result)

    def test_logout_failure_does_not_mask_prior_error(self):
        self.responses[PATHS[4]] = httpx.ReadTimeout(SECRET)
        self.assertEqual(self.run_probe()['reason_code'], 'LOGOUT_FAILED')
        self.responses[PATHS[2]] = envelope({'required': True, 'version': 'v'})
        result = self.run_probe()
        self.assertEqual(result['reason_code'], 'COMPLIANCE_REQUIRED')
        self.assertEqual(result['logout'], 'failed')
        self.assert_safe(result)

    def test_client_disables_environment_and_redirects(self):
        original = httpx.AsyncClient
        with patch.object(self.subject.httpx, 'AsyncClient', wraps=original) as client:
            self.run_probe()
        kwargs = client.call_args.kwargs
        self.assertIs(kwargs['trust_env'], False)
        self.assertIs(kwargs['follow_redirects'], False)
        self.assertIs(kwargs['verify'], True)
        self.assertGreater(kwargs['timeout'], 0)

    def test_invalid_group_is_rejected_before_network(self):
        for group in ('', ' ', 'x\nx', None, True):
            result = self.subject.run_probe(str(self.config), group, transport=httpx.MockTransport(self.handler))
            self.assertEqual(result['reason_code'], 'INVALID_ARGUMENTS')
        self.assertEqual(self.seen, [])

    def test_cli_redacts_argument_and_config_errors(self):
        script = str(ROOT / 'tools/onboarding_sub2api_probe.py')
        for args in (['--help'], ['--unknown=' + SECRET], ['--config', SECRET],
                     ['--config', str(self.config) + SECRET, '--group', 'fixture-gemini']):
            result = subprocess.run([sys.executable, script, *args], cwd=ROOT,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr, '')
            self.assertNotIn(SECRET, result.stdout)
            self.assert_safe(json.loads(result.stdout))

    def test_hardlinked_config_rejected_before_network(self):
        os.link(self.config, self.directory / 'second-link.json')
        self.reject_config()

    def test_compressed_response_rejected_before_decompression(self):
        import gzip
        self.responses[PATHS[0]] = httpx.Response(200,
            stream=httpx.ByteStream(gzip.compress(b'{"status":"ok"}')),
            headers={'content-type': 'application/json', 'content-encoding': 'gzip'})
        result = self.run_probe()
        self.assertEqual(result['reason_code'], 'HEALTH_FAILED')
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0].headers['accept-encoding'], 'identity')

    def test_slow_drip_has_total_response_deadline(self):
        clock = [0.0]
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(inner):
                for _ in range(20):
                    clock[0] += 3.0
                    yield b' '
                yield b'{"status":"ok"}'
            async def aclose(inner):
                inner.closed = True
        stream = Stream()
        stream.closed = False
        self.responses[PATHS[0]] = httpx.Response(200, stream=stream,
                                                headers={'content-type': 'application/json'})
        with patch.object(self.subject, 'monotonic', side_effect=lambda: clock[0], create=True):
            result = self.run_probe()
        self.assertEqual(result['reason_code'], 'HEALTH_FAILED')
        self.assertLessEqual(clock[0], 12.0)
        self.assertTrue(stream.closed)
        self.assertEqual(len(self.seen), 1)

    def test_invalid_httpx_url_is_redacted_without_network(self):
        self.rewrite(url='https://[v9.x]')
        self.reject_config()
        with self.assertRaises(self.subject.ProbeError) as caught:
            self.subject.load_config(str(self.config))
        self.assertEqual(str(caught.exception), 'INVALID_CONFIG')
        self.assertNotIn('v9.x', repr(caught.exception))

    def test_streaming_limit_without_content_length_closes_stream(self):
        class Stream(httpx.AsyncByteStream):
            def __init__(inner):
                inner.closed = False
                inner.reads = 0
            async def __aiter__(inner):
                for _ in range(30):
                    inner.reads += 1
                    yield b'x' * 65536
            async def aclose(inner):
                inner.closed = True
        stream = Stream()
        self.responses[PATHS[1]] = httpx.Response(200, stream=stream,
                                                headers={'content-type': 'application/json'})
        result = self.run_probe()
        self.assertEqual(result['reason_code'], 'AUTH_FAILED')
        self.assertTrue(stream.closed)
        self.assertEqual(stream.reads, 17)
        self.assertEqual([r.url.path for r in self.seen], PATHS[:2])

    def test_duplicate_json_response_fields_rejected(self):
        self.responses[PATHS[2]] = httpx.Response(200, content=(
            b'{"code":0,"data":{"required":true,"required":false,"version":"v"}}'),
            headers={'content-type': 'application/json'})
        result = self.run_probe()
        self.assertEqual(result['reason_code'], 'COMPLIANCE_FAILED')
        self.assertEqual(result['logout'], 'passed')
        self.assertNotIn(PATHS[3], [r.url.path for r in self.seen])

    def test_post_login_http_error_still_attempts_logout_once(self):
        for response in (httpx.Response(401), httpx.Response(423),
                         httpx.Response(302, headers={'location': 'https://leak.invalid'}),
                         httpx.ReadTimeout(SECRET)):
            self.seen.clear()
            self.responses[PATHS[2]] = response
            result = self.run_probe()
            self.assertEqual(result['reason_code'], 'COMPLIANCE_FAILED')
            self.assertEqual(result['logout'], 'passed')
            self.assertEqual([r.url.path for r in self.seen], PATHS[:3] + [PATHS[-1]])

    def test_logout_schema_is_strict_and_never_ready_on_failure(self):
        for response in ({'code': False, 'data': None}, {'code': 1, 'data': None},
                         {'code': 0}, [], httpx.Response(401)):
            self.responses[PATHS[-1]] = response
            result = self.run_probe()
            self.assertEqual(result['reason_code'], 'LOGOUT_FAILED')
            self.assertEqual(result['logout'], 'failed')
            self.assert_safe(result)

    def test_valid_config_limit_and_response_limit_are_inclusive(self):
        raw = self.config.read_bytes()
        self.config.write_bytes(raw + b' ' * (16384 - len(raw)))
        health = b'{"status":"ok"}'
        self.responses[PATHS[0]] = httpx.Response(200,
            content=health + b' ' * (1024 * 1024 - len(health)),
            headers={'content-type': 'application/json'})
        self.assertEqual(self.run_probe()['reason_code'], 'READY')

    def test_real_header_slow_drip_deadline_and_logout_cleanup(self):
        import time
        seen = []
        responses = self.responses
        slow_paths = set()
        disconnected = threading.Event()
        early_hints = [False]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner):
                seen.append(inner.path)
                if inner.path in slow_paths:
                    try:
                        if not early_hints[0]:
                            inner.connection.sendall(b'HTTP/1.0 200 OK\r\nX-Slow: ')
                        for _ in range(20):
                            time.sleep(0.05)
                            inner.connection.sendall(b'HTTP/1.1 103 Early Hints\r\n\r\n'
                                                     if early_hints[0] else b'x')
                    except (BrokenPipeError, ConnectionResetError):
                        disconnected.set()
                    return
                payload = json.dumps(responses[inner.path]).encode()
                inner.send_response(200)
                inner.send_header('Content-Type', 'application/json')
                inner.send_header('Content-Length', str(len(payload)))
                inner.end_headers()
                inner.wfile.write(payload)
            def do_POST(inner):
                inner.rfile.read(int(inner.headers.get('Content-Length', 0)))
                inner.do_GET()
            def log_message(inner, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.rewrite(url='http://127.0.0.1:' + str(server.server_port))
            cases = [({PATHS[0]}, 'HEALTH_FAILED', 'not_required', PATHS[:1]),
                     ({PATHS[2]}, 'COMPLIANCE_FAILED', 'passed', PATHS[:3] + [PATHS[-1]]),
                     ({PATHS[4]}, 'LOGOUT_FAILED', 'failed', PATHS),
                     ({PATHS[2], PATHS[4]}, 'COMPLIANCE_FAILED', 'failed', PATHS[:3] + [PATHS[-1]]),
                     ({PATHS[0]}, 'HEALTH_FAILED', 'not_required', PATHS[:1])]
            for index, (paths, reason, logout, expected) in enumerate(cases):
                with self.subTest(stage=reason, logout=logout, early_hints=index == 4):
                    early_hints[0] = index == 4
                    slow_paths.clear()
                    slow_paths.update(paths)
                    seen.clear()
                    disconnected.clear()
                    start = time.monotonic()
                    with patch.object(self.subject, 'TIMEOUT_SECONDS', 0.15):
                        result = self.subject.run_probe(str(self.config), 'fixture-gemini',
                                                        allow_loopback_http=True)
                    elapsed = time.monotonic() - start
                    self.assertLess(elapsed, 0.65, 'header drip exceeded whole-request deadline')
                    self.assertEqual(result['reason_code'], reason)
                    self.assertEqual(result['logout'], logout)
                    self.assertEqual(seen, expected)
                    self.assertTrue(disconnected.wait(0.4), 'timed out socket was not closed')
                    self.assert_safe(result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_self_signed_tls_is_rejected_before_login(self):
        import datetime
        import ipaddress
        import ssl
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'fixture-localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
        cert_path = self.directory / 'fixture-cert.pem'
        key_path = self.directory / 'fixture-key.pem'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner):
                seen.append(inner.path)
                inner.send_response(200)
                inner.end_headers()
            do_POST = do_GET
            def log_message(inner, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.rewrite(url='https://127.0.0.1:' + str(server.server_port))
            result = self.subject.run_probe(str(self.config), 'fixture-gemini')
            self.assertEqual(result['reason_code'], 'HEALTH_FAILED')
            self.assertEqual(result['auth'], 'not_checked')
            self.assertEqual(seen, [])
            self.assert_safe(result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_loopback_transport_and_proxy_environment_ignored(self):
        seen = self.seen
        responses = self.responses
        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner):
                seen.append(inner.path)
                payload = json.dumps(responses[inner.path]).encode()
                inner.send_response(200)
                inner.send_header('Content-Type', 'application/json')
                inner.send_header('Content-Length', str(len(payload)))
                inner.end_headers()
                inner.wfile.write(payload)
            def do_POST(inner):
                inner.rfile.read(int(inner.headers.get('Content-Length', 0)))
                inner.do_GET()
            def log_message(inner, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.rewrite(url='http://127.0.0.1:' + str(server.server_port))
            env = dict(os.environ, HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1',
                       ALL_PROXY='http://127.0.0.1:1', NO_PROXY='', SSL_CERT_FILE='/missing/fixture-ca')
            result = subprocess.run([sys.executable, str(ROOT / 'tools/onboarding_sub2api_probe.py'),
                                     '--config', str(self.config), '--group', 'fixture-gemini',
                                     '--allow-loopback-http'], cwd=ROOT, env=env, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr, '')
            payload = json.loads(result.stdout)
            self.assertEqual(payload['reason_code'], 'READY')
            self.assert_safe(payload)
            self.assertEqual(seen, PATHS)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
