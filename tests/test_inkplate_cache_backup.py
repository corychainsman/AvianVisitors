"""Isolated HTTP integration tests; requires Docker and a local PHP 8.1+ image.

INKPLATE_PHP_IMAGE defaults to avian-visitors-birdnet:local. No live application
container or data is changed; a temporary container and /config are removed.
"""
import hashlib
import http.client
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEVICE = '012345abcdef'
OFFSET = 0x1910000


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker'):
            raise unittest.SkipTest('Docker required')
        image = os.environ.get('INKPLATE_PHP_IMAGE', 'avian-visitors-birdnet:local')
        if subprocess.run(['docker', 'image', 'inspect', image], capture_output=True).returncode:
            raise unittest.SkipTest('Local PHP test image unavailable')
        cls.temp = tempfile.TemporaryDirectory(prefix='inkplate-backup-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.config = pathlib.Path(cls.temp.name)
        cls.name = 'inkplate-backup-test-' + uuid.uuid4().hex[:10]
        subprocess.run(['docker', 'run', '-d', '--rm', '--name', cls.name,
                        '--user', f'{os.getuid()}:{os.getgid()}', '-p', '127.0.0.1::8080', '-v', f'{ROOT / "avian/api"}:/srv:ro',
                        '-v', f'{cls.config}:/config', '--entrypoint', 'php', image,
                        '-d', 'display_errors=0', '-S', '0.0.0.0:8080', '-t', '/srv'],
                       check=True, capture_output=True)
        cls.addClassCleanup(lambda: subprocess.run(['docker', 'stop', '-t', '1', cls.name], capture_output=True))
        cls.port = int(subprocess.check_output(['docker', 'port', cls.name, '8080/tcp'], text=True).strip().rsplit(':', 1)[1])
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.1):
                    return
            except OSError:
                time.sleep(.05)
        raise RuntimeError('PHP test server did not start')

    def setUp(self):
        # Keep the test registry while resetting only isolated backup files.
        subprocess.run(['docker', 'exec', self.name, 'php', '-r',
                        '$d="/config/InkplateCacheBackups"; foreach(glob($d."/{*,.*}",GLOB_BRACE)?:[] as $p) if(is_file($p)) unlink($p);'],
                       check=True, capture_output=True)
        status = self.config / 'InkplateOTA/status'
        status.mkdir(parents=True, exist_ok=True)
        (status / f'{DEVICE}.json').write_text(json.dumps({'device': DEVICE}))
        self.backups = self.config / 'InkplateCacheBackups'

    def request(self, data=b'partition contents\x00\xff', changes=None, method='POST'):
        digest = hashlib.sha256(data).hexdigest()
        headers = {'Host': '127.0.0.1', 'Content-Type': 'application/octet-stream',
                   'X-Inkplate-Device': DEVICE, 'X-Backup-Sha256': digest,
                   'X-Backup-Offset': str(OFFSET), 'Content-Length': str(len(data))}
        headers.update(changes or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.request(method, '/inkplate-cache-backup.php', body=data, headers=headers)
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def test_backup_receipt_metadata_and_idempotence(self):
        data = b'\x01\xfe' * (0x6c0000 // 2)
        digest = hashlib.sha256(data).hexdigest()
        expected = (200, f'VERIFIED {digest} {len(data)}\n')
        self.assertEqual(self.request(data), expected)
        path = self.backups / f'{DEVICE}-{digest}.bin'
        self.assertEqual(path.read_bytes(), data)
        metadata = json.loads(path.with_suffix('.bin.json').read_text())
        self.assertEqual((metadata['offset'], metadata['length'], metadata['sha256']), (OFFSET, len(data), digest))
        before = path.stat().st_mtime_ns
        self.assertEqual(self.request(data), expected)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(self.request(data, {'X-Backup-Offset': '0'})[0], 409)
        self.assertEqual(path.read_bytes(), data)

    def test_invalid_headers_and_access_do_not_create_backups(self):
        for changes, code in [({'X-Inkplate-Device': '../bad'}, 400),
                              ({'X-Inkplate-Device': 'aaaaaaaaaaaa'}, 403),
                              ({'X-Backup-Sha256': 'nothex'}, 400),
                              ({'X-Backup-Offset': '-1'}, 400),
                              ({'X-Backup-Offset': '4294967295'}, 400),
                              ({'Content-Type': 'text/plain'}, 400),
                              ({'Host': 'public.example.com'}, 403),
                              ({'X-Forwarded-Host': 'public.example.com'}, 403),
                              ({'Host': '192.168.attacker.com'}, 403),
                              ({'X-Backup-Offset': '99999999999'}, 400)]:
            with self.subTest(changes=changes):
                self.assertEqual(self.request(changes=changes)[0], code)
        self.assertEqual(self.request(method='GET')[0], 405)
        self.assertEqual(self.request(b'x' * 8388609)[0], 413)
        self.assertEqual(list(self.backups.glob('*.bin')), [])

    def test_mismatched_hash_cleanup_preserves_existing(self):
        data = b'original'
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.request(data)[0], 200)
        self.assertEqual(self.request(b'bad data', {'X-Backup-Sha256': digest})[0], 400)
        self.assertEqual(self.request(b'bad data', {'X-Backup-Sha256': '0' * 64})[0], 400)
        self.assertEqual((self.backups / f'{DEVICE}-{digest}.bin').read_bytes(), data)
        self.assertEqual(list(self.backups.glob('.upload-*')), [])

    def test_backup_limit_preserves_both_files(self):
        self.assertEqual(self.request(b'one')[0], 200)
        self.assertEqual(self.request(b'two')[0], 200)
        self.assertEqual(self.request(b'three')[0], 429)
        self.assertEqual(self.request(b'one')[0], 200)
        self.assertEqual(sorted(p.read_bytes() for p in self.backups.glob('*.bin')), [b'one', b'two'])

    def test_backup_not_downloadable(self):
        data = b'private partition'
        self.assertEqual(self.request(data)[0], 200)
        filename = f'{DEVICE}-{hashlib.sha256(data).hexdigest()}.bin'
        for path in [f'/InkplateOTA/cache-backups/{filename}', f'/InkplateCacheBackups/{filename}']:
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
            try:
                connection.request('GET', path)
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                self.assertNotIn(data, response.read())
            finally:
                connection.close()

    def test_corrupt_existing_file_never_verified_or_overwritten(self):
        data = b'original'
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.request(data)[0], 200)
        path = self.backups / f'{DEVICE}-{digest}.bin'
        path.write_bytes(b'corrupt!')
        self.assertEqual(self.request(data)[0], 503)
        self.assertEqual(path.read_bytes(), b'corrupt!')
        self.assertEqual(list(self.backups.glob('.upload-*')), [])

    def test_truncated_body_never_verified(self):
        data = b'partial'
        digest = hashlib.sha256(data).hexdigest()
        request = (f'POST /inkplate-cache-backup.php HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                   f'Content-Type: application/octet-stream\r\nContent-Length: 100\r\n'
                   f'X-Inkplate-Device: {DEVICE}\r\nX-Backup-Sha256: {digest}\r\n'
                   f'X-Backup-Offset: {OFFSET}\r\nConnection: close\r\n\r\n').encode() + data
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            response = b''
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response += chunk
        self.assertNotIn(b'VERIFIED', response)
        self.assertEqual(list(self.backups.glob('*.bin')), [])


if __name__ == '__main__':
    unittest.main()
