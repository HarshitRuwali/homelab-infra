# Reading the output

Ansible is verbose. Almost all of it is noise once you know which four lines
matter.

## The shape of a run

```text
PLAY [Deploy the Alloy collector] **********************   ← a play begins
TASK [Gathering Facts] *********************************   ← a task
ok: [rpi5]                                                 ← per-host result
TASK [alloy_collector : Install Alloy] *****************   ← role : task name
ok: [rpi5]
RUNNING HANDLER [alloy_collector : restart alloy] ******   ← a handler fired
changed: [rpi5]
PLAY RECAP *********************************************   ← the summary
rpi5   : ok=20  changed=3  unreachable=0  failed=0
```

`TASK [rolename : task name]` tells you which role a task came from, which is
how you find it in the files.

## Task statuses

| Status | Colour | Meaning | Worry? |
|---|---|---|---|
| `ok` | green | already correct, nothing done | no |
| `changed` | yellow | was wrong, now fixed | only if unexpected |
| `skipping` | cyan | a `when:` condition was false | no |
| `failed` | red | could not be done | yes |
| `fatal` | red | failed, and the play stops for this host | yes |
| `unreachable` | red | could not SSH in at all | yes |
| `rescued` | | failed but a `rescue:` block handled it | no |
| `ignored` | | failed but `ignore_errors: true` was set | maybe |

!!! tip "`skipping` is normal and healthy"
    Most `skipping` lines here are capability checks. A host with no Docker
    skips every Docker task. That is the role working correctly, not a
    problem.

## The PLAY RECAP

The only part you must read.

```text
PLAY RECAP *********************************************************************
cloud-services  : ok=19  changed=1  unreachable=0  failed=0  skipped=1  rescued=0  ignored=0
matrix          : ok=19  changed=1  unreachable=0  failed=0  skipped=1  rescued=0  ignored=0
rpi4b           : ok=18  changed=0  unreachable=0  failed=0  skipped=2  rescued=0  ignored=0
```

Read it in this order:

1. **`unreachable`** must be 0. Anything else means a host was never touched,
   so it is not configured and you do not know its state.
2. **`failed`** must be 0.
3. **`changed`** should match your expectation. On a first run, high. On a
   repeat run, **zero**.
4. `ok` and `skipped` are informational.

!!! danger "`unreachable=1` is worse than `failed=1`"
    A failure tells you something specific went wrong. Unreachable means
    Ansible never got in, so that machine silently did not get any of the
    changes the others did. It is the one that leaves your fleet
    inconsistent without saying so.

### Getting just the recap

Playbook output is long. To see only the summary:

```bash
ansible-playbook playbooks/site.yml | sed -n '/PLAY RECAP/,$p'
```

## Timing output

This repo enables the profiling callbacks, so each run ends with a
slowest-tasks list:

```text
TASKS RECAP ********************************************************************
Gathering Facts ------------------------------------------ 3.42s
alloy_collector : Run Alloy as root on Docker hosts ------- 3.21s
alloy_collector : Write the Alloy configuration ----------- 2.32s
```

Useful for spotting a task that has quietly become slow, usually one doing
`apt` work on an SD-card Pi.

## When something fails

A failure prints the module's own error:

```text
fatal: [ubuntu-dev]: FAILED! => {
  "changed": false,
  "msg": "Task failed: Finalization of task args for 'ansible.builtin.file' failed:
          Error while resolving value for 'path': 'update_metrics_dir' is undefined"
}
```

Work backwards:

1. **Which host?** `[ubuntu-dev]`.
2. **Which task?** The `TASK [...]` line immediately above.
3. **What does `msg` say?** Here: a variable does not exist. In this real case
   it was because roles do not share each other's defaults.

### Common failures in this repo

??? failure "`'<var>' is undefined`"
    A variable is used but never defined for that host. Check whether it lives
    in a **different role's** defaults, since roles do not see each other's.

    ```bash
    ansible-inventory --host <hostname>   # what IS defined
    ```

??? failure "`Permission denied (publickey)`"
    Not necessarily a key problem. Without `IdentitiesOnly=yes`, SSH offers
    every key it has and a host with a low `MaxAuthTries` hangs up first. Also
    check `ansible_user` for that host: this fleet uses `harshit`, `sp00f` and
    `root` on different machines.

??? failure "`Missing sudo password`"
    The host does not have passwordless sudo. Every play here uses
    `become: true`. Fix with NOPASSWD sudo, or add
    `ansible_become_password` to the vault for that host.

    `--ask-become-pass` cannot help across this fleet, because the hosts use
    different users with different passwords.

??? failure "`Syntax error in template: unexpected '.'`"
    Jinja tried to render something that was not meant for it. Almost always a
    Go template such as `docker ps --format '{{.Names}}'` passed to
    `-m shell`. Put it in a script file and use `-m script`.

??? failure "`mapping values are not allowed in this context`"
    A YAML indentation error. Count the spaces on the line **above** the one
    named. Never use tabs.

??? failure "A task reports `changed` on every single run"
    Not fatal, but a real bug. Usually a `file:` task with `recurse: true`
    re-fixing ownership of files another process keeps writing, or a `shell:`
    used where a proper module exists.

## Verbosity

```bash
ansible-playbook playbooks/site.yml -v      # show module return values
ansible-playbook playbooks/site.yml -vvv    # + the actual SSH commands
```

`-vvv` is the tool for "why can it not connect": it prints the full `ssh`
invocation, so you can copy it and run it yourself.

!!! warning "`no_log: true` hides output on purpose"
    Tasks handling secrets print `the output has been hidden due to the fact
    that 'no_log: true' was specified`. That is intentional, and `-vvv` will
    not reveal it. Do not remove `no_log` to debug; the value is in the vault.

## Interpreting a partial run

If one host fails partway, the others carry on. The recap will show a mix:

```text
rpi5        : ok=20  changed=3  unreachable=0  failed=0
rpi4b       : ok=4   changed=0  unreachable=0  failed=1
```

`rpi4b` stopped at task 5. It is now in a **half-configured** state: whatever
ran before the failure was applied, whatever came after was not.

The fix is almost always: correct the cause, then re-run the same playbook
with `--limit rpi4b`. Because tasks are idempotent, the ones that already
succeeded report `ok` and it picks up where it stopped.

## What "changed" does not tell you

`changed` means Ansible modified something. It does **not** mean the service
is healthy. That is why roles here end with an explicit check:

```yaml
- name: Wait for Alloy to become ready
  ansible.builtin.uri:
    url: "http://127.0.0.1:12345/-/ready"
    status_code: [200]
  retries: 10
  delay: 3
```

An `ok` on **that** task is the real proof.

!!! bug "A related trap in shell checks"
    `systemctl is-active alloy | grep -q active` matches **`inactive`** too.
    Compare the exact string. This once caused a rollout to be reported
    complete when nothing had been installed.
