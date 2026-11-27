<?php
/*
 * Add the internal interfaces to NetFlow capture (Reporting > NetFlow, listening interfaces),
 * so segflows sees every flow between two segments.
 *
 *   php netflow-capture.php [--interfaces lan,opt2,opt3] [--apply]
 *   php netflow-capture.php --revert [--apply]
 *
 * Run from /usr/local/opnsense/mvc. Dry run by default: prints the current and the new list,
 * validates through the Netflow model, saves nothing.
 *
 * Only adds: every interface already in the list stays. --interfaces replaces the default set
 * to add (lan,opt2,opt3 = LAN, DMZ, LAB). --revert sets the list back to opt1,opt4 (TENANT and
 * LEASE, the list before segflows). The WAN is left out on purpose: a flow to the home LAN or the
 * internet is seen on its internal interface. The collector target (127.0.0.1:2056) and the
 * version are not touched.
 *
 * --apply saves and then does what the GUI's "Apply" does: restart the NetFlow netgraph nodes
 * (netflow stop, template reload, netflow start), restart the aggregator, and start the
 * collector if it is not running. Restarting the nodes drops flows that are still open in the
 * nodes' caches (up to 15 s idle or 30 min active), so expect a short gap in the counters.
 */
require_once 'script/load_phalcon.php';
use OPNsense\Core\Config;

const ORIGINAL = ['opt1', 'opt4'];
const DEFAULT_ADD = ['lan', 'opt2', 'opt3'];

$apply = in_array('--apply', $argv, true);
$revert = in_array('--revert', $argv, true);
$add = DEFAULT_ADD;
$known = ['--apply', '--revert', '--interfaces'];
for ($i = 1; $i < count($argv); $i++) {
    if ($argv[$i] === '--interfaces') {
        if (!isset($argv[$i + 1]) || $argv[$i + 1] === '' || substr($argv[$i + 1], 0, 2) === '--') {
            fwrite(STDERR, "--interfaces needs a comma-separated list, for example lan,opt2,opt3\n");
            exit(2);
        }
        $add = array_values(array_filter(array_map('trim', explode(',', $argv[$i + 1])), 'strlen'));
        $i++;
    } elseif (!in_array($argv[$i], $known, true)) {
        fwrite(STDERR, "usage: php netflow-capture.php [--interfaces lan,opt2,opt3] [--apply] | --revert [--apply]\n");
        exit(2);
    }
}
if ($revert && in_array('--interfaces', $argv, true)) {
    fwrite(STDERR, "--revert and --interfaces cannot be combined\n");
    exit(2);
}

Config::getInstance()->lock();
$m = new \OPNsense\Diagnostics\Netflow();
$current = (string)$m->capture->interfaces === '' ? [] : explode(',', (string)$m->capture->interfaces);
$new = $revert ? ORIGINAL : array_values(array_unique(array_merge($current, $add)));

echo "targets (unchanged): " . (string)$m->capture->targets . ", version " . (string)$m->capture->version . "\n";
echo "before: " . implode(',', $current) . "\n";
echo "after:  " . implode(',', $new) . "\n";
if ($new === $current) {
    echo "nothing to change\n";
    exit(0);
}
$m->capture->interfaces = implode(',', $new);
$errors = [];
foreach ($m->performValidation() as $msg) { $errors[] = $msg->getField() . ': ' . $msg->getMessage(); }
if ($errors) { fwrite(STDERR, "validation failed:\n  " . implode("\n  ", $errors) . "\n"); exit(1); }
if (!$apply) { echo "dry run OK, nothing saved\n"; exit(0); }
$m->serializeToConfig();
Config::getInstance()->save();
$configctl = '/usr/local/sbin/configctl';
echo shell_exec("$configctl netflow stop");
echo shell_exec("$configctl template reload OPNsense/Netflow");
echo shell_exec("$configctl netflow start");
if ((string)$m->collect->enable === '1') {
    if (strpos(shell_exec("$configctl netflow collect status"), 'not running') !== false) {
        echo shell_exec("$configctl netflow collect start");
    }
    echo shell_exec("$configctl netflow aggregate restart");
}
echo shell_exec("$configctl netflow status");
