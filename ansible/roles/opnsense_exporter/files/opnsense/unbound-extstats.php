<?php
/* Unbound: turn on extended statistics (needed by the exporter).  [--apply] */
require_once 'script/load_phalcon.php';
use OPNsense\Core\Config;
$apply = in_array('--apply', $argv, true);
Config::getInstance()->lock();
$u = new \OPNsense\Unbound\Unbound();
$before = (string)$u->advanced->extendedstatistics;
$u->advanced->extendedstatistics = '1';
$errors = [];
foreach ($u->performValidation() as $msg) { $errors[] = $msg->getField() . ': ' . $msg->getMessage(); }
if ($errors) { fwrite(STDERR, "validation failed:\n  " . implode("\n  ", $errors) . "\n"); exit(1); }
echo "advanced.extendedstatistics: $before -> 1\n";
if (!$apply) { echo "dry run OK, nothing saved\n"; exit(0); }
$u->serializeToConfig();
Config::getInstance()->save();
echo shell_exec('/usr/local/sbin/configctl unbound restart');
