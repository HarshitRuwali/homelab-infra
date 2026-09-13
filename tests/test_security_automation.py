"""Local regression tests. External commands are fakes; no host is contacted.

Run with: python3 -m unittest discover -s tests -v
Dependencies: Jinja2 and PyYAML (also supplied by the Ansible controller venv).
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import xml.etree.ElementTree as ET
import tempfile
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure", ROOT / "security/install/_configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class ConfigurationTests(unittest.TestCase):
    def test_wazuh_preserves_multiple_sections_and_enables_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ossec.conf"
            path.write_text('<ossec_config><auth><use_password>no</use_password></auth>'
                            '<localfile><location>journald</location></localfile></ossec_config>'
                            '<ossec_config><auth><use_password>no</use_password></auth>'
                            '<syscheck><frequency>120</frequency></syscheck></ossec_config>')
            path.chmod(0o640)
            configure.configure_wazuh(path)
            result = path.read_text()
            self.assertEqual(result.count('<use_password>yes</use_password>'), 2)
            self.assertIn('<location>journald</location>', result)
            self.assertIn('<frequency>120</frequency>', result)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            configure.configure_wazuh(path)
            self.assertEqual(path.read_text(), result)

    def test_password_validation_accepts_only_one_nonempty_strong_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            for invalid in ('short', 'a' * 20 + '\n\n', 'a' * 20 + ' b'):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    configure.check_password(path)
            path.write_text('a' * 32 + '\n')
            configure.check_password(path)

    def test_crowdsec_updates_server_and_local_client_without_losing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            credentials = Path(directory) / "credentials.yaml"
            config.write_text('api:\n  server:\n    listen_uri: 127.0.0.1:8080\n'
                              '  client:\n    insecure_skip_verify: true\ndb_config:\n  type: sqlite\n')
            credentials.write_text('url: http://localhost:8080\nlogin: local\npassword: test-secret\n')
            configure.configure_crowdsec(config, credentials, 'https://lapi.example:8080')
            result = yaml.safe_load(config.read_text())
            client = yaml.safe_load(credentials.read_text())
            self.assertEqual(result['db_config']['type'], 'sqlite')
            self.assertEqual(result['api']['server']['listen_uri'], '0.0.0.0:8080')
            self.assertIn('cert_file', result['api']['server']['tls'])
            self.assertNotIn('insecure_skip_verify', result['api']['client'])
            self.assertEqual(client['password'], 'test-secret')
            self.assertEqual(client['url'], 'https://lapi.example:8080')
            self.assertIn('ca_cert_path', client)
            with self.assertRaises(ValueError):
                configure.configure_crowdsec(config, credentials, 'http://lapi.example:8080')


class ProvisioningTests(unittest.TestCase):
    def probe(self, kind, failure):
        source = (ROOT / 'security/provision-security-stack.sh').read_text().split('\nmain() {')[0]
        # Definitions and shell builtins only: qm and pct are always intercepted.
        fake = r'''
APPLY=1
qm() { printf '%s\n' "qm $*" >> "$REVIEW_LOG"; [[ "$1" != "$REVIEW_FAILURE" ]]; }
pct() { printf '%s\n' "pct $*" >> "$REVIEW_LOG"; [[ "$1" != "$REVIEW_FAILURE" ]]; }
'''
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'commands'
            env = dict(os.environ, REVIEW_LOG=str(log), REVIEW_FAILURE=failure)
            result = subprocess.run(['bash', '-c', source + fake +
                                     f'\ncreate_{kind} review 999 1 512 8 ssd trusted\n'],
                                    env=env, text=True, capture_output=True)
            return result, log.read_text()

    def test_vm_nested_failure_cleans_up_once(self):
        result, log = self.probe('vm', 'set')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log.count('qm destroy 999 --purge'), 1)

    def test_lxc_nested_failure_cleans_up_once(self):
        result, log = self.probe('lxc', 'start')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log.count('pct destroy 999 --purge'), 1)

    def test_create_collision_never_destroys_existing_guest(self):
        for kind in ('vm', 'lxc'):
            with self.subTest(kind=kind):
                result, log = self.probe(kind, 'create')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('destroy', log)


class DockerUpdaterTests(unittest.TestCase):
    def probe(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            bin_dir = directory / 'bin'
            bin_dir.mkdir()
            base = directory / 'compose.yaml'
            override = directory / 'override.yaml'
            base.write_text('services: {}')
            if mode != 'missing_override':
                override.write_text('services: {}')
            fake_docker = bin_dir / 'docker'
            fake_docker.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['REVIEW_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
mode = os.environ['REVIEW_MODE']
if args[0] == 'ps':
    if mode == 'discovery_failure': sys.exit(1)
    if any('health=' in a or 'status=' in a for a in args): sys.exit(0)
    print('test-id')
elif args[0] == 'inspect':
    if 'Config.Labels' in args[-1]:
        files = '' if mode == 'missing_labels' else os.environ['REVIEW_FILES']
        print('project|' + os.environ['REVIEW_DIR'] + '|' + files)
    else: print('sha256:old')
elif args[0] == 'compose':
    if 'pull' in args and mode == 'pull_failure': sys.exit(1)
    if 'up' in args and mode == 'health_failure': sys.exit(1)
elif args[0] == 'image': print('Total reclaimed space: 0B')
''')
            fake_docker.chmod(0o755)
            env = jinja2.Environment(undefined=jinja2.StrictUndefined)
            env.filters['ternary'] = lambda value, yes, no: yes if value else no
            script = env.from_string((ROOT / 'ansible/roles/docker_updates/templates/fleet-docker-update.sh.j2').read_text()).render(
                inventory_hostname='review', update_metrics_dir=str(directory),
                docker_update_metrics_file='result.prom', docker_update_skip_projects=[],
                docker_update_prune=True, docker_update_health_wait_seconds=0)
            log = directory / 'commands'
            process_env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                               REVIEW_LOG=str(log), REVIEW_MODE=mode, REVIEW_DIR=str(directory),
                               REVIEW_FILES=f'{base},{override}')
            result = subprocess.run(['bash'], input=script, env=process_env, text=True, capture_output=True)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            metrics = (directory / 'result.prom').read_text()
            return result, commands, metrics

    def test_success_requires_all_files_and_does_not_ignore_registry_errors(self):
        result, commands, metrics = self.probe('success')
        self.assertEqual(result.returncode, 0, result.stderr)
        pull = next(c for c in commands if 'pull' in c)
        self.assertIn('--ignore-buildable', pull)
        self.assertNotIn('--ignore-pull-failures', pull)
        self.assertEqual(pull.count('-f'), 2)
        self.assertIn('fleet_docker_update_failed 0', metrics)

    def test_missing_override_or_labels_prevents_pull_and_redeployment(self):
        for mode in ('missing_override', 'missing_labels'):
            with self.subTest(mode=mode):
                result, commands, metrics = self.probe(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[0] == 'compose' for c in commands))
                self.assertIn('fleet_docker_update_failed 1', metrics)

    def test_registry_failure_never_redeploys_or_prunes(self):
        result, commands, metrics = self.probe('pull_failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('up' in c or c[0] == 'image' for c in commands))
        self.assertIn('fleet_docker_update_failed 1', metrics)

    def test_discovery_and_readiness_failures_report_failure(self):
        for mode in ('discovery_failure', 'health_failure'):
            with self.subTest(mode=mode):
                result, commands, metrics = self.probe(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[0] == 'image' for c in commands))
                self.assertIn('fleet_docker_update_failed 1', metrics)


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible controller tools not installed')
class AnsiblePolicyTests(unittest.TestCase):
    def run_ansible(self, directory, args):
        config = directory / 'ansible.cfg'
        config.write_text('[defaults]\nroles_path = ' + str(ROOT / 'ansible/roles') + '\n')
        environment = dict(os.environ, ANSIBLE_CONFIG=str(config))
        for key in ('ANSIBLE_VAULT_PASSWORD_FILE', 'ANSIBLE_VAULT_IDENTITY_LIST'):
            environment.pop(key, None)
        result = subprocess.run(['ansible-playbook'] + args, env=environment,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_opt_out_targets_removed_and_explicitly_excluded_hosts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            inventory = directory / 'inventory.yml'
            inventory.write_text(yaml.safe_dump({'all': {'children': {
                'monitored': {'hosts': {'patched': {}, 'removed': {}, 'overlap': {}}},
                'autoupdate': {'hosts': {'patched': {}, 'overlap': {}}},
                'no_autoupdate': {'hosts': {'overlap': {}, 'explicit': {}}},
            }}}))
            output = self.run_ansible(directory, ['--list-hosts', '-i', str(inventory),
                                                   str(ROOT / 'ansible/playbooks/docker-updates.yml')])
            cleanup, install = output.split('  play #2 ')
            for name in ('removed', 'overlap', 'explicit'):
                self.assertIn('      ' + name, cleanup)
                self.assertNotIn('      ' + name, install)
            self.assertIn('      patched', install)
            self.assertNotIn('      patched', cleanup)

    def test_blacklist_regexes_resolve_to_installed_packages_and_preserve_holds(self):
        playbook = yaml.safe_load((ROOT / 'ansible/playbooks/force-updates.yml').read_text())
        resolve = next(task for task in playbook[0]['tasks']
                       if task.get('name') == 'Resolve temporary holds without changing existing pins')
        play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': {
            'ansible_facts': {'packages': {'raspberrypi-kernel': [], 'raspberrypi-bootloader': [],
                                         'linux-image-rpi-v8': [], 'unrelated': []}},
            'uu_package_blacklist': ['raspberrypi-kernel', 'raspberrypi-bootloader', 'linux-image-rpi-.*'],
            'force_update_original_holds': {'stdout_lines': ['raspberrypi-kernel']},
            'force_update_respect_blacklist': True,
        }, 'tasks': [resolve, {'ansible.builtin.assert': {'that': [
            "force_update_temporary_holds == ['linux-image-rpi-v8', 'raspberrypi-bootloader']"
        ]}}]}]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / 'policy.yml'
            path.write_text(yaml.safe_dump(play))
            self.run_ansible(directory, ['-i', 'localhost,', str(path)])


class AgentTemplateTests(unittest.TestCase):
    def test_enrollment_verifies_manager_and_collects_journal_events(self):
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        template = environment.from_string((ROOT / 'ansible/roles/wazuh_agent/templates/ossec.conf.j2').read_text())
        rendered = template.render(wazuh_manager_address='manager.example',
            wazuh_manager_event_port=1514, wazuh_manager_enrollment_port=1515,
            inventory_hostname='agent', wazuh_agent_group='homelab',
            wazuh_manager_ca_path='/var/ossec/etc/manager-ca.pem',
            wazuh_enrollment_password_path='/var/ossec/etc/enrollment.pass',
            wazuh_fim_directories=['/etc'],
            wazuh_log_sources=[{'location': 'journald', 'log_format': 'journald'},
                               {'location': '/var/log/app&worker.log', 'log_format': 'syslog'}])
        root = ET.fromstring(rendered)
        enrollment = root.find('client/enrollment')
        self.assertEqual(enrollment.findtext('server_ca_path'), '/var/ossec/etc/manager-ca.pem')
        self.assertEqual(enrollment.findtext('authorization_pass_path'), '/var/ossec/etc/enrollment.pass')
        self.assertEqual(root.findtext('localfile/location'), 'journald')
        self.assertIn('/var/log/app&worker.log', [node.text for node in root.findall('localfile/location')])


class DownloadTests(unittest.TestCase):
    def test_failed_download_never_runs_the_next_command(self):
        source = (ROOT / 'security/install/_common.sh').read_text()
        script = source + '''
APPLY=1
curl() { return 22; }
mv() { echo SHOULD_NOT_RENAME; }
download_file https://example.invalid/script /unused
printf SHOULD_NOT_EXECUTE
'''
        result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('SHOULD_NOT', result.stdout)


if __name__ == '__main__':
    unittest.main()
