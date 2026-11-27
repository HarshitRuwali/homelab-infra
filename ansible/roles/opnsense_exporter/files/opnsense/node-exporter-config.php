<?php
/* Enable node_exporter on the LAN address only.  php node-exporter-config.php [--apply] */
require_once 'script/load_phalcon.php';
use OPNsense\Core\Config;
$apply = in_array('--apply', $argv, true);
Config::getInstance()->lock();
$m = new \OPNsense\NodeExporter\General();
$want = ['enabled' => '1', 'listenaddress' => '10.10.50.1', 'listenport' => '9100',
         'cpu' => '1', 'exec' => '1', 'filesystem' => '1', 'loadavg' => '1', 'meminfo' => '1',
         'netdev' => '1', 'time' => '1', 'devstat' => '1', 'interrupts' => '0', 'ntp' => '0', 'zfs' => '0'];
$before = [];
foreach ($want as $k => $v) { $before[$k] = (string)$m->$k; $m->$k = $v; }
$errors = [];
foreach ($m->performValidation() as $msg) { $errors[] = $msg->getField() . ': ' . $msg->getMessage(); }
if ($errors) { fwrite(STDERR, "validation failed:\n  " . implode("\n  ", $errors) . "\n"); exit(1); }
echo "before: " . json_encode($before) . "\nafter:  " . json_encode($want) . "\n";
if (!$apply) { echo "dry run OK, nothing saved\n"; exit(0); }
$m->serializeToConfig();
Config::getInstance()->save();
echo shell_exec('/usr/local/sbin/configctl template reload OPNsense/NodeExporter');
echo shell_exec('/usr/local/sbin/configctl node_exporter restart');
