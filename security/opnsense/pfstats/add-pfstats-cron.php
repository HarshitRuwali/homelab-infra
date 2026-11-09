<?php
// Adds the every-minute pfstats cron job. usage: php add-pfstats-cron.php dry|apply
require_once('script/load_phalcon.php');

use OPNsense\Core\Config;
use OPNsense\Cron\Cron;

$mode = $argv[1] ?? 'dry';
$c = new Cron();
foreach ($c->jobs->job->iterateItems() as $uuid => $j) {
    if ((string)$j->command === 'pfstats log') {
        echo "already present: $uuid\n";
        exit(0);
    }
}
$j = $c->jobs->job->add();
$j->setNodes([
    'enabled' => '1', 'minutes' => '*', 'hours' => '*', 'days' => '*', 'months' => '*',
    'weekdays' => '*', 'who' => 'root', 'command' => 'pfstats log', 'parameters' => '',
    'description' => 'pfstats: pf state table and interface counters to syslog every minute',
]);
$msgs = [];
foreach ($c->performValidation() as $m) { $msgs[] = $m->getField() . ': ' . $m->getMessage(); }
if (count($msgs)) { echo "VALIDATION FAILED\n  " . implode("\n  ", $msgs) . "\n"; exit(3); }
echo "validation: ok\n";
if ($mode === 'apply') {
    $c->serializeToConfig();
    Config::getInstance()->save();
    echo "SAVED (run: configctl template reload OPNsense/Cron; configctl cron restart)\n";
} else {
    echo "DRY RUN: nothing saved\n";
}
