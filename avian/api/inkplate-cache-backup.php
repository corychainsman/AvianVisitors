<?php
declare(strict_types=1);

// Rescue an unmountable flash filesystem before firmware formats it. This
// endpoint has no read/delete operation: backups remain available to operators.
header('Content-Type: text/plain; charset=utf-8');
header('Cache-Control: no-store');
function failBackup(int $code, string $message): never {
    http_response_code($code);
    echo $message . "\n";
    exit;
}
function privateAddress(string $address): bool {
    if (!filter_var($address, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4)) return $address === '::1';
    $parts = array_map('intval', explode('.', $address));
    return $parts[0] === 10 || $parts[0] === 127 ||
        ($parts[0] === 192 && $parts[1] === 168) ||
        ($parts[0] === 172 && $parts[1] >= 16 && $parts[1] <= 31);
}
function privateHost(string $value): bool {
    $host = strtolower(explode(':', trim($value))[0]);
    return privateAddress($host) || $host === 'localhost' ||
        preg_match('/^[a-z0-9.-]+\.local$/', $host) === 1;
}
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') failBackup(405, 'POST required');
if (!privateAddress((string) ($_SERVER['REMOTE_ADDR'] ?? '')) ||
    !privateHost((string) ($_SERVER['HTTP_HOST'] ?? '')) ||
    (isset($_SERVER['HTTP_X_FORWARDED_HOST']) && !privateHost($_SERVER['HTTP_X_FORWARDED_HOST']))) {
    failBackup(403, 'private-network only');
}
$device = strtolower((string) ($_SERVER['HTTP_X_INKPLATE_DEVICE'] ?? ''));
$hash = strtolower((string) ($_SERVER['HTTP_X_BACKUP_SHA256'] ?? ''));
$lengthText = (string) ($_SERVER['CONTENT_LENGTH'] ?? '');
$offsetText = (string) ($_SERVER['HTTP_X_BACKUP_OFFSET'] ?? '');
if (!preg_match('/^[0-9a-f]{12}$/', $device) || !preg_match('/^[0-9a-f]{64}$/', $hash) ||
    !preg_match('/^[1-9][0-9]{0,7}$/', $lengthText) ||
    !preg_match('/^(0|[1-9][0-9]{0,9})$/', $offsetText) ||
    strtolower(trim(explode(';', (string) ($_SERVER['CONTENT_TYPE'] ?? ''))[0])) !== 'application/octet-stream' ||
    isset($_SERVER['HTTP_TRANSFER_ENCODING'])) failBackup(400, 'invalid backup headers');
$length = (int) $lengthText;
$offset = (int) $offsetText;
if ($length > 8 * 1024 * 1024) failBackup(413, 'backup too large');
if ($offset + $length > 4294967296) failBackup(400, 'invalid partition range');
$root = '/config/InkplateOTA';
$record = json_decode((string) @file_get_contents($root . '/status/' . $device . '.json'), true);
if (!is_array($record) || ($record['device'] ?? '') !== $device) failBackup(403, 'unregistered device');
$directory = '/config/InkplateCacheBackups';
if (!is_dir($directory) && !@mkdir($directory, 0750, true) && !is_dir($directory)) failBackup(503, 'storage unavailable');
$lock = @fopen($directory . '/.lock', 'c');
if (!$lock || !flock($lock, LOCK_EX)) failBackup(503, 'storage lock unavailable');
$path = $directory . '/' . $device . '-' . $hash . '.bin';
$metadataPath = $path . '.json';
$existing = is_file($path);
if (!$existing && count(glob($directory . '/' . $device . '-*.bin') ?: []) >= 2) failBackup(429, 'device backup limit reached');
if ($existing) {
    $metadata = json_decode((string) @file_get_contents($metadataPath), true);
    if (!is_array($metadata) || ($metadata['offset'] ?? null) !== $offset ||
        ($metadata['length'] ?? null) !== $length || ($metadata['sha256'] ?? '') !== $hash ||
        ($metadata['device'] ?? '') !== $device) failBackup(409, 'existing backup metadata mismatch');
}
$temporary = tempnam($directory, '.upload-');
$metadataTemp = null;
if ($temporary === false) failBackup(503, 'temporary storage unavailable');
register_shutdown_function(static function () use (&$temporary, &$metadataTemp): void {
    if ($temporary !== null) @unlink($temporary);
    if ($metadataTemp !== null) @unlink($metadataTemp);
});
$output = @fopen($temporary, 'wb');
$input = @fopen('php://input', 'rb');
if (!$output || !$input) failBackup(503, 'stream unavailable');
$received = 0;
$context = hash_init('sha256');
while (!feof($input)) {
    $chunk = fread($input, min(65536, $length - $received + 1));
    if ($chunk === false || ($chunk === '' && !feof($input))) failBackup(400, 'incomplete request');
    if ($chunk === '') break;
    $received += strlen($chunk);
    if ($received > $length) failBackup(400, 'length mismatch');
    hash_update($context, $chunk);
    if (fwrite($output, $chunk) !== strlen($chunk)) failBackup(503, 'backup write failed');
}
fclose($input);
if ($received !== $length || !hash_equals($hash, hash_final($context))) failBackup(400, 'length or hash mismatch');
if (!fflush($output) || !fsync($output)) failBackup(503, 'backup sync failed');
fclose($output);
if (!$existing) {
    $metadataTemp = tempnam($directory, '.metadata-');
    if ($metadataTemp === false) failBackup(503, 'metadata storage unavailable');
    $metadataFile = @fopen($metadataTemp, 'wb');
    $encoded = json_encode(['device' => $device, 'offset' => $offset, 'length' => $length,
        'sha256' => $hash, 'saved_at' => gmdate('c')], JSON_PRETTY_PRINT) . "\n";
    if (!$metadataFile || fwrite($metadataFile, $encoded) !== strlen($encoded) ||
        !fflush($metadataFile) || !fsync($metadataFile)) failBackup(503, 'metadata sync failed');
    fclose($metadataFile);
    if (!rename($metadataTemp, $metadataPath)) failBackup(503, 'metadata commit failed');
    $metadataTemp = null;
    if (!rename($temporary, $path)) failBackup(503, 'backup commit failed');
    $temporary = null;
}
// Read back the committed file and flush its directory entry before issuing the
// receipt that permits a destructive repair on the device.
$directoryHandle = @fopen($directory, 'r');
if (!$directoryHandle || !fsync($directoryHandle)) failBackup(503, 'directory sync failed');
fclose($directoryHandle);
clearstatcache(true, $path);
if (filesize($path) !== $length || !hash_equals($hash, (string) hash_file('sha256', $path))) failBackup(503, 'saved backup verification failed');
echo 'VERIFIED ' . $hash . ' ' . $length . "\n";
