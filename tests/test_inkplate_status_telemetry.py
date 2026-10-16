"""Status telemetry HTTP contract; uses the isolated PHP Docker test fixture."""
import concurrent.futures
import copy
import http.client
import json
import unittest
import urllib.parse

try:
    from . import test_inkplate_cache_backup as backup_http
except ImportError:
    import test_inkplate_cache_backup as backup_http

DEVICE = '012345abcdef'
DURATIONS = ['boot_to_setup_us', 'setup_to_menu_us', 'cache_us',
             'graphics_init_us', 'draw_us', 'driver_prepare_us', 'panel_init_us',
             'transfer_us', 'refresh_us', 'poweroff_us', 'driver_total_us',
             'menu_total_us', 'boot_to_ready_us']
SAMPLE = dict(schema=1, firmware_build=2026101603, captured_at=1792152000,
              driver_hooks=True, **{key: 1000 + i for i, key in enumerate(DURATIONS)})


class StatusTelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backup_http.BackupTests.setUpClass.__func__(cls)

    def setUp(self):
        self.status_directory = self.config / 'InkplateOTA/status'
        self.status_directory.mkdir(parents=True, exist_ok=True)
        for path in self.status_directory.iterdir():
            path.unlink()
        self.status_path = self.status_directory / f'{DEVICE}.json'

    def request(self, fields=None, method='POST'):
        payload = dict(device=DEVICE, build='2026101603', version='test',
                       status='online', detail='status telemetry test', rssi='-45')
        payload.update(fields or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            body = urllib.parse.urlencode(payload) if method == 'POST' else None
            connection.request(method, '/inkplate-status.php', body=body,
                               headers={'Host': '127.0.0.1',
                                        'Content-Type': 'application/x-www-form-urlencoded'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def send_sample(self, sample):
        return self.request({'menu_opening': json.dumps(sample)})

    def test_valid_sample_readback_and_replacement(self):
        self.assertEqual(self.send_sample(SAMPLE), (200, {'ok': True}))
        self.assertEqual(json.loads(self.status_path.read_text())['menu_opening'], SAMPLE)
        code, result = self.request(method='GET')
        self.assertEqual(code, 200)
        self.assertEqual(result['devices'][0]['menu_opening'], SAMPLE)
        next_sample = dict(SAMPLE, cache_us=20000, driver_hooks=False, captured_at=0)
        self.assertEqual(self.send_sample(next_sample)[0], 200)
        self.assertEqual(json.loads(self.status_path.read_text())['menu_opening'], next_sample)

    def test_legacy_status_preserves_last_sample(self):
        self.assertEqual(self.request()[0], 200)
        self.assertNotIn('menu_opening', json.loads(self.status_path.read_text()))
        self.assertEqual(self.send_sample(SAMPLE)[0], 200)
        self.assertEqual(self.request({'status': 'sleeping', 'build': '2026101604'})[0], 200)
        saved = json.loads(self.status_path.read_text())
        self.assertEqual(saved['menu_opening'], SAMPLE)
        self.assertEqual(saved['status'], 'sleeping')
        self.assertEqual(saved['build'], 2026101604)

    def test_missing_fields_unknown_keys_and_invalid_types_preserve_sample(self):
        self.assertEqual(self.send_sample(SAMPLE)[0], 200)
        original = self.status_path.read_bytes()
        cases = []
        for key in SAMPLE:
            value = copy.copy(SAMPLE)
            del value[key]
            cases.append(value)
        cases += [dict(SAMPLE, schema=2), dict(SAMPLE, schema=True),
                  dict(SAMPLE, extra='unexpected'), dict(SAMPLE, firmware_build=-1),
                  dict(SAMPLE, firmware_build=4294967296), dict(SAMPLE, firmware_build='123'),
                  dict(SAMPLE, captured_at=-1), dict(SAMPLE, captured_at=1),
                  dict(SAMPLE, captured_at=4294967296), dict(SAMPLE, driver_hooks=1),
                  dict(SAMPLE, driver_hooks='true'), None, [], 'sample']
        for key in DURATIONS:
            for invalid in [-1, 300000001, 1.5, True, '10', None]:
                cases.append(dict(SAMPLE, **{key: invalid}))
        for sample in cases:
            with self.subTest(sample=sample):
                self.assertEqual(self.send_sample(sample)[0], 400)
                self.assertEqual(self.status_path.read_bytes(), original)

    def test_bad_json_oversized_and_form_array_rejected(self):
        for raw in ['', '{', 'null', '{"schema":1}', ' ' * 2049,
                    json.dumps(SAMPLE) + (' ' * 2048)]:
            with self.subTest(raw=raw[:30]):
                self.assertEqual(self.request({'menu_opening': raw})[0], 400)
                self.assertFalse(self.status_path.exists())
        self.assertEqual(self.request({'menu_opening[]': 'unexpected'})[0], 400)
        self.assertFalse(self.status_path.exists())

    def test_boundary_values(self):
        for limit in (0, 300000000):
            sample = dict(SAMPLE, **{key: limit for key in DURATIONS})
            sample.update(firmware_build=4294967295, captured_at=4294967295)
            self.assertEqual(self.send_sample(sample)[0], 200)
            self.assertEqual(json.loads(self.status_path.read_text())['menu_opening'], sample)

    def test_interleaved_legacy_status_cannot_erase_sample(self):
        self.assertEqual(self.send_sample(SAMPLE)[0], 200)
        updated = dict(SAMPLE, cache_us=9000)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            requests = [executor.submit(self.request, {'status': 'sleeping'}) for _ in range(8)]
            requests.append(executor.submit(self.send_sample, updated))
            requests += [executor.submit(self.request, {'status': 'online'}) for _ in range(8)]
            for request in requests:
                self.assertEqual(request.result()[0], 200)
        self.assertEqual(json.loads(self.status_path.read_text())['menu_opening'], updated)


if __name__ == '__main__':
    unittest.main()
