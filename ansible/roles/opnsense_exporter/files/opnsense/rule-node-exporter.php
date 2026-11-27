<?php
/* LAN rule: monitor-lxc may scrape node_exporter on the firewall.  [--apply] */
require_once 'script/load_phalcon.php';
use OPNsense\Core\Config;
$apply = in_array('--apply', $argv, true);
$desc = 'sandbox: monitor-lxc scrapes node_exporter on the firewall';
Config::getInstance()->lock();
$f = new \OPNsense\Firewall\Filter();
$anchor = null;
foreach ($f->rules->rule->iterateItems() as $uuid => $r) {
    if ((string)$r->description === $desc) { fwrite(STDERR, "rule exists ($uuid); nothing done\n"); exit(2); }
    if ((string)$r->description === 'sandbox: DNS on OPNsense') { $anchor = $r; }
}
if ($anchor === null) { fwrite(STDERR, "anchor rule not found; nothing done\n"); exit(1); }
$r = $f->rules->rule->Add();
$vals = ['enabled' => '1', 'action' => 'pass', 'quick' => '1', 'interface' => 'lan', 'direction' => 'in',
         'ipprotocol' => 'inet', 'protocol' => 'TCP', 'source_net' => '10.10.50.185', 'destination_net' => 'lanip',
         'destination_port' => '9100', 'log' => '0', 'sequence' => (string)((int)(string)$anchor->sequence + 1),
         'description' => $desc];
foreach ($vals as $k => $v) { $r->$k = $v; }
$errors = [];
foreach ($f->performValidation() as $msg) { $errors[] = $msg->getField() . ': ' . $msg->getMessage(); }
if ($errors) { fwrite(STDERR, "validation failed:\n  " . implode("\n  ", $errors) . "\n"); exit(1); }
echo "anchor sequence " . $anchor->sequence . "; new rule: " . json_encode($vals) . "\n";
if (!$apply) { echo "dry run OK, nothing saved\n"; exit(0); }
$f->serializeToConfig();
Config::getInstance()->save();
echo shell_exec('/usr/local/sbin/configctl filter reload');
