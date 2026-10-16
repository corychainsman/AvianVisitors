<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

$statusDir = '/config/InkplateOTA/status';

if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    $items = [];
    foreach (glob($statusDir . '/*.json') ?: [] as $path) {
        $decoded = json_decode((string) file_get_contents($path), true);
        if (is_array($decoded)) {
            $items[] = $decoded;
        }
    }
    usort($items, static fn(array $a, array $b): int =>
        strcmp((string) ($b['seen_at'] ?? ''), (string) ($a['seen_at'] ?? ''))
    );
    echo json_encode(['devices' => $items], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'method not allowed']);
    exit;
}

// Status writes are only needed on the Inkplate's private backend URL. Keep
// the read-only status view available through Traefik, but reject writes that
// arrived through the public hostname to prevent internet-driven disk churn.
$host = strtolower(explode(':', (string) ($_SERVER['HTTP_HOST'] ?? ''))[0]);
$privateHost = $host === 'localhost' || $host === '127.0.0.1' ||
    str_ends_with($host, '.local') ||
    preg_match('/^10\./', $host) === 1 ||
    preg_match('/^192\.168\./', $host) === 1 ||
    preg_match('/^172\.(1[6-9]|2[0-9]|3[01])\./', $host) === 1;
if (!$privateHost) {
    http_response_code(403);
    echo json_encode(['error' => 'status writes are private-network only']);
    exit;
}

$device = strtolower(trim((string) ($_POST['device'] ?? '')));
$build = trim((string) ($_POST['build'] ?? ''));
$version = substr(trim((string) ($_POST['version'] ?? '')), 0, 80);
$status = substr(trim((string) ($_POST['status'] ?? '')), 0, 40);
$detail = substr(trim((string) ($_POST['detail'] ?? '')), 0, 200);
$rssi = trim((string) ($_POST['rssi'] ?? ''));

if (!preg_match('/^[0-9a-f]{12}$/', $device) ||
    !preg_match('/^[0-9]{1,10}$/', $build) ||
    !preg_match('/^[a-z0-9-]{1,40}$/', $status)) {
    http_response_code(400);
    echo json_encode(['error' => 'invalid status payload']);
    exit;
}

// Each sample describes one completed menu opening. Keep the schema strict so
// malformed/partial instrumentation cannot replace the last usable measurement.
function validMenuOpening($sample): bool {
    $durations = ['boot_to_setup_us', 'setup_to_menu_us', 'cache_us',
        'graphics_init_us', 'draw_us', 'driver_prepare_us', 'panel_init_us',
        'transfer_us', 'refresh_us', 'poweroff_us', 'driver_total_us',
        'menu_total_us', 'boot_to_ready_us'];
    $keys = array_merge(['schema', 'firmware_build', 'captured_at', 'driver_hooks'], $durations);
    if (!is_array($sample) || count($sample) !== count($keys) ||
        array_diff($keys, array_keys($sample)) || ($sample['schema'] ?? null) !== 1 ||
        !is_int($sample['firmware_build']) || $sample['firmware_build'] < 0 ||
        $sample['firmware_build'] > 4294967295 || !is_int($sample['captured_at']) ||
        ($sample['captured_at'] !== 0 && ($sample['captured_at'] < 1704067200 ||
            $sample['captured_at'] > 4294967295)) || !is_bool($sample['driver_hooks'])) return false;
    foreach ($durations as $key) {
        if (!is_int($sample[$key]) || $sample[$key] < 0 || $sample[$key] > 300000000) return false;
    }
    return true;
}

$menuOpening = null;
if (array_key_exists('menu_opening', $_POST)) {
    $raw = $_POST['menu_opening'];
    if (!is_string($raw) || strlen($raw) > 2048 ||
        !validMenuOpening($menuOpening = json_decode($raw, true))) {
        http_response_code(400);
        echo json_encode(['error' => 'invalid menu opening telemetry']);
        exit;
    }
}

if (!is_dir($statusDir) && !mkdir($statusDir, 0755, true) && !is_dir($statusDir)) {
    http_response_code(503);
    echo json_encode(['error' => 'status storage unavailable']);
    exit;
}

$path = $statusDir . '/' . $device . '.json';
if (!is_file($path) && count(glob($statusDir . '/*.json') ?: []) >= 8) {
    http_response_code(429);
    echo json_encode(['error' => 'device status limit reached']);
    exit;
}

// Serialize the read/modify/write so a simultaneous status post without a
// sample cannot overwrite telemetry just saved by another request.
$lock = @fopen($path . '.lock', 'c');
if (!$lock || !flock($lock, LOCK_EX)) {
    http_response_code(503);
    echo json_encode(['error' => 'status storage lock unavailable']);
    exit;
}
if ($menuOpening === null && is_file($path)) {
    $previous = json_decode((string) @file_get_contents($path), true);
    if (is_array($previous) && validMenuOpening($previous['menu_opening'] ?? null)) {
        $menuOpening = $previous['menu_opening'];
    }
}

$record = [
    'device' => $device,
    'build' => (int) $build,
    'version' => $version,
    'status' => $status,
    'detail' => $detail,
    'rssi' => preg_match('/^-?[0-9]{1,3}$/', $rssi) ? (int) $rssi : null,
    'seen_at' => gmdate('c'),
];
if ($menuOpening !== null) $record['menu_opening'] = $menuOpening;
$temporary = $path . '.tmp';
$encoded = json_encode($record, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
if (file_put_contents($temporary, $encoded, LOCK_EX) === false || !rename($temporary, $path)) {
    @unlink($temporary);
    http_response_code(503);
    echo json_encode(['error' => 'could not store status']);
    exit;
}

echo json_encode(['ok' => true]);
