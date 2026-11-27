<?php
/*
 * Create a read-only API user for the opnsense-exporter (Prometheus), the way
 * the GUI's user manager does (OPNsense\Auth\User model, ApiKeyField::add()).
 *
 *   php create-exporter-user.php            dry run: build and validate, save nothing
 *   php create-exporter-user.php --apply    save, sync the user, print {"key","secret"}
 *
 * Only stdout carries the key pair, so the caller redirects it into a private
 * file. Its password is scrambled: the user cannot log in to the web GUI, only use its key.
 */
require_once 'script/load_phalcon.php';

use OPNsense\Auth\User;
use OPNsense\Core\Config;

$apply = in_array('--apply', $argv, true);
$name = 'prometheus-exporter';
$privs = [
    'page-diagnostics-arptable',          // Diagnostics: ARP Table
    'page-diagnostics-pf-info',           // Diagnostics: Firewall statistics
    'page-diagnostics-netstat',           // Diagnostics: Netstat
    'page-status-trafficgraph',           // Reporting: Traffic
    'page-services-unbound',              // Services: Unbound
    'page-status-dnsoverview',            // Status: DNS Overview
    'page-status-services',               // Status: Services
    'page-system-firmware-manualupdate',  // System: Firmware
    'page-system-gateways',               // System: Gateways
    'page-system-cron',                   // System: Settings: Cron
    'page-system-status',                 // System: Status
    'page-system-login-logout',           // Lobby: Dashboard (temperature, pf states, mbuf)
    'page-dhcp-kea-v4',                   // Services: DHCP: Kea(v4)
];

Config::getInstance()->lock();
$mdl = new User();
foreach ($mdl->user->iterateItems() as $u) {
    if ((string)$u->name === $name) {
        fwrite(STDERR, "user $name already exists; nothing done\n");
        exit(2);
    }
}
$u = $mdl->user->Add();
$u->name = $name;
$u->descr = 'Read-only API user for the opnsense-exporter (Prometheus)';
$u->priv = implode(',', $privs);
// A random password nobody knows, hashed, as the GUI's "scrambled password"
// does in UserController::setBaseHook (the model alone would save none).
$password = random_bytes(50);
while (($i = strpos($password, "\0")) !== false) {
    $password[$i] = random_bytes(1);
}
$hash = $mdl->generatePasswordHash($password);
if ($hash === false || strpos($hash, '$') !== 0) {
    fwrite(STDERR, "could not hash a password; nothing done\n");
    exit(1);
}
$u->password = $hash;
$u->pwd_changed_at = microtime(true);
$secret = $u->apikeys->add();

$errors = [];
foreach ($mdl->performValidation() as $msg) {
    $errors[] = $msg->getField() . ': ' . $msg->getMessage();
}
if ($errors) {
    fwrite(STDERR, "validation failed:\n  " . implode("\n  ", $errors) . "\n");
    exit(1);
}
if (!$apply) {
    fwrite(STDERR, sprintf("dry run OK: would create user %s (uid %s) with %d privileges and one API key\n",
        $name, (string)$u->uid, count($privs)));
    exit(0);
}
$mdl->serializeToConfig();
Config::getInstance()->save();
shell_exec('/usr/local/sbin/configctl auth sync user ' . escapeshellarg($name));
fwrite(STDERR, sprintf("created user %s (uid %s)\n", $name, (string)$u->uid));
echo json_encode(['key' => $secret['key'], 'secret' => $secret['secret']]);
