<?php
// Adds or removes the every-5-minutes segflows cron job.
// usage: php add-segflows-cron.php dry|apply|remove
require_once('script/load_phalcon.php');

use OPNsense\Core\Config;
use OPNsense\Cron\Cron;

$mode = $argv[1] ?? 'dry';
if (!in_array($mode, ['dry', 'apply', 'remove'], true)) {
    fwrite(STDERR, "usage: php add-segflows-cron.php dry|apply|remove\n");
    exit(2);
}
$c = new Cron();
$found = null;
foreach ($c->jobs->job->iterateItems() as $uuid => $j) {
    if ((string)$j->command === 'segflows run') {
        $found = $uuid;
        break;
    }
}
if ($mode === 'remove') {
    if ($found === null) {
        echo "not present: nothing to remove\n";
        exit(0);
    }
    $c->jobs->job->del($found);
    $c->serializeToConfig();
    Config::getInstance()->save();
    echo "REMOVED $found (run: configctl template reload OPNsense/Cron; configctl cron restart)\n";
    exit(0);
}
if ($found !== null) {
    echo "already present: $found\n";
    exit(0);
}
$j = $c->jobs->job->add();
$j->setNodes([
    'enabled' => '1', 'minutes' => '*/5', 'hours' => '*', 'days' => '*', 'months' => '*',
    'weekdays' => '*', 'who' => 'root', 'command' => 'segflows run', 'parameters' => '',
    'description' => 'segflows: NetFlow traffic between network segments, counters and ledger, every 5 minutes',
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
