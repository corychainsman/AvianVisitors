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

$record = [
    'device' => $device,
    'build' => (int) $build,
    'version' => $version,
    'status' => $status,
    'detail' => $detail,
    'rssi' => preg_match('/^-?[0-9]{1,3}$/', $rssi) ? (int) $rssi : null,
    'seen_at' => gmdate('c'),
];
$temporary = $path . '.tmp';
$encoded = json_encode($record, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
if (file_put_contents($temporary, $encoded, LOCK_EX) === false || !rename($temporary, $path)) {
    @unlink($temporary);
    http_response_code(503);
    echo json_encode(['error' => 'could not store status']);
    exit;
}

echo json_encode(['ok' => true]);
