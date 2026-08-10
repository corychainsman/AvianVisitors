<?php
// AvianVisitors train-clip review API.
// Prefer the generated interval manifest when it is available.

declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

$stateDir = '/config/train-review';
$persistentCandidatePath = $stateDir . '/candidates.json';
$bundledCandidatePath = dirname(__DIR__) . '/train-review/candidates.json';
$candidatePath = is_readable($persistentCandidatePath) ? $persistentCandidatePath : $bundledCandidatePath;
$statePath = $stateDir . '/labels.json';
$auditPath = $stateDir . '/labels.ndjson';

function respond(int $status, array $body): never {
    http_response_code($status);
    echo json_encode($body, JSON_UNESCAPED_SLASHES);
    exit;
}

function read_candidates(string $path): array {
    $decoded = is_readable($path) ? json_decode((string)file_get_contents($path), true) : null;
    return is_array($decoded) ? $decoded : [];
}

function read_labels(string $path): array {
    $decoded = is_readable($path) ? json_decode((string)file_get_contents($path), true) : null;
    return is_array($decoded) ? $decoded : [];
}

$candidates = read_candidates($candidatePath);
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

if ($method === 'GET') {
    respond(200, ['candidates' => $candidates, 'labels' => read_labels($statePath)]);
}

if ($method !== 'POST') {
    header('Allow: GET, POST');
    respond(405, ['error' => 'method not allowed']);
}

// Reject cross-origin browser writes. This is deliberately a low-friction
// household review tool; labels are also appended to an audit log so an
// accidental or unwanted change remains recoverable.
$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
$host = preg_replace('/:\d+$/', '', (string)($_SERVER['HTTP_HOST'] ?? ''));
if ($origin !== '') {
    $originHost = (string)(parse_url($origin, PHP_URL_HOST) ?? '');
    if ($originHost === '' || strcasecmp($originHost, $host) !== 0) {
        respond(403, ['error' => 'cross-origin write rejected']);
    }
}

$body = json_decode((string)file_get_contents('php://input'), true);
$id = is_array($body) ? (string)($body['id'] ?? '') : '';
$label = is_array($body) ? (string)($body['label'] ?? '') : '';
$allowedLabels = ['train', 'not_train', 'unsure'];
$candidateIds = array_column($candidates, 'id');
if (!in_array($id, $candidateIds, true) || !in_array($label, $allowedLabels, true)) {
    respond(400, ['error' => 'invalid candidate or label']);
}

$rangeStart = is_array($body) && array_key_exists('start_seconds', $body)
    ? filter_var($body['start_seconds'], FILTER_VALIDATE_FLOAT)
    : null;
$rangeEnd = is_array($body) && array_key_exists('end_seconds', $body)
    ? filter_var($body['end_seconds'], FILTER_VALIDATE_FLOAT)
    : null;
$hasRange = $rangeStart !== null && $rangeStart !== false
    && $rangeEnd !== null && $rangeEnd !== false;
if ($hasRange && ($label !== 'train' || $rangeStart < 0 || $rangeEnd > 120 || $rangeEnd - $rangeStart < 0.5)) {
    respond(400, ['error' => 'invalid train sound range']);
}

if (!is_dir($stateDir) && !mkdir($stateDir, 0755, true) && !is_dir($stateDir)) {
    respond(500, ['error' => 'unable to create label directory']);
}

$lockPath = $stateDir . '/labels.lock';
$lock = fopen($lockPath, 'c+');
if ($lock === false || !flock($lock, LOCK_EX)) {
    respond(500, ['error' => 'unable to lock labels']);
}

$labels = read_labels($statePath);
$entry = [
    'label' => $label,
    'updated_at' => date(DATE_ATOM),
];
if ($hasRange) {
    // Millisecond precision is more than enough for acoustic-model windows,
    // while keeping the hand-edited JSON compact and readable.
    $entry['start_seconds'] = round((float)$rangeStart, 3);
    $entry['end_seconds'] = round((float)$rangeEnd, 3);
}
$labels[$id] = $entry;
$tmp = $statePath . '.tmp.' . getmypid();
$encoded = json_encode($labels, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
if (file_put_contents($tmp, $encoded) === false || !rename($tmp, $statePath)) {
    flock($lock, LOCK_UN);
    fclose($lock);
    @unlink($tmp);
    respond(500, ['error' => 'unable to save label']);
}

$audit = json_encode(['id' => $id] + $entry, JSON_UNESCAPED_SLASHES) . "\n";
file_put_contents($auditPath, $audit, FILE_APPEND | LOCK_EX);
flock($lock, LOCK_UN);
fclose($lock);

respond(200, ['ok' => true, 'id' => $id, 'entry' => $entry]);
