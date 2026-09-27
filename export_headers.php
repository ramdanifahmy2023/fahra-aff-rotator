<?php
/** Export the latest captured affiliate GraphQL headers for the local worker. */

$root = '/Volumes/DATA KERJA /Project Dev/sniper/xyzsniper';
$envFile = $root . '/config/.env';
$env = [];
foreach (file($envFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [] as $line) {
    if (str_starts_with(trim($line), '#') || !str_contains($line, '=')) continue;
    [$key, $value] = explode('=', $line, 2);
    $env[trim($key)] = trim($value, " \t\"'");
}
$pdo = new PDO(
    'mysql:host=' . ($env['DB_HOST_LOCAL'] ?? '127.0.0.1') .
    ';port=' . ($env['DB_PORT_LOCAL'] ?? '3308') .
    ';dbname=' . ($env['DB_NAME_LOCAL'] ?? 'xyzsniper_db'),
    $env['DB_USER_LOCAL'] ?? 'root',
    $env['DB_PASS_LOCAL'] ?? ''
);
$pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
$stmt = $pdo->query("SELECT request_header FROM endpoints WHERE project_id=2 AND path='/api/v3/gql' ORDER BY updated_at DESC LIMIT 1");
$headers = json_decode((string) $stmt->fetchColumn(), true);
if (!is_array($headers) || !$headers) {
    fwrite(STDERR, "Header /api/v3/gql belum tersedia. Buka ulang alur custom link di Chrome.\n");
    exit(1);
}
$target = __DIR__ . '/Credential/request_headers.json';
if (!is_dir(dirname($target))) mkdir(dirname($target), 0700, true);
file_put_contents($target, json_encode($headers, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n");
chmod($target, 0600);
echo "Header terbaru diekspor ke {$target}\n";
