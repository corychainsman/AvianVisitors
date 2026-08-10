<?php

declare(strict_types=1);

// Convert BirdNET-Pi's maintained scientific-name -> eBird species-code
// table into a browser-friendly JSON asset. Run this whenever scripts/ebird.php
// is updated; the live dashboard never needs to call the eBird API.
$source = dirname(__DIR__, 2) . '/scripts/ebird.php';
$output = dirname(__DIR__) . '/frontend/ebird-codes.json';

if (!is_file($source)) {
    fwrite(STDERR, "Missing eBird code table: {$source}\n");
    exit(1);
}

$ebirds = [];
require $source;

if (!is_array($ebirds) || $ebirds === []) {
    fwrite(STDERR, "No eBird codes loaded from {$source}\n");
    exit(1);
}

ksort($ebirds, SORT_STRING);
$json = json_encode(
    $ebirds,
    JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR
);
file_put_contents($output, $json . "\n");

fwrite(STDOUT, 'wrote ' . count($ebirds) . " eBird codes to {$output}\n");
