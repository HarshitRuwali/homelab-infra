<?php

/*
 * Registers the "pfstats" syslog program so it can be selected as a source
 * application in a remote syslog destination (System > Settings > Logging).
 * The program itself is /usr/local/sbin/pfstats-log.sh, run every minute by
 * a cron job that calls the configd action "pfstats log".
 */

function pfstats_syslog()
{
    $logfacilities = [];

    $logfacilities['pfstats'] = ['facility' => ['pfstats']];

    return $logfacilities;
}
