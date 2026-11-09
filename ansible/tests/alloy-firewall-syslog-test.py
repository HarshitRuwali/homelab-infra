#!/usr/bin/env python3
"""Test the firewall syslog pipeline of roles/alloy_collector.

Run from anywhere:

    python3 -I ansible/tests/alloy-firewall-syslog-test.py

Two layers. The first needs only the standard library and always runs; it reads
the template text:
  * the listener's static labels carry job="firewall-other", and the relabel
    rule copies the syslog program name into job only for suricata, filterlog,
    unbound and pfstats
  * the iface and action labels are set BEFORE the drop stage, the drop selects
    on iface through alloy_syslog_drop_filterlog_iface, and the rate limit does
    not select on an interface
  * alloy_syslog_drop_filterlog_iface defaults to vtnet0
  * group_vars/central drops the default deny and NOT the firewall's own
    outbound rule, whose line carries the outbound leg of every tenant flow
  * the iface and action labels are taken only from fixed sets of values, in
    one regex stage per field

The second runs the real thing. It needs jinja2 and the `alloy` binary and is
skipped, loudly, without them. It renders the syslog section of the template
with jinja2 the way Ansible does, runs `alloy validate` on it, starts Alloy
with that config on loopback, sends it RFC 5424 syslog over TCP and reads what
it pushes to a fake Loki on loopback:
  * the rule list of group_vars/central is dropped on the WAN interface and the
    default deny is kept on every other; the firewall's own outbound rule is
    kept everywhere, WAN included
  * the iface and action labels are present on what is kept, for every allowed
    value, and a forged value (any text, a third digit, a long suffix) gets no
    label while the line is still stored
  * suricata, unbound and pfstats keep their name as job; any other sender
    name, including near misses of an allowed one, is stored as
    job="firewall-other"
  * the rate limit applies on every interface, and caps a flood
  * the listener's own labels reach every stream
"""
import http.server
import json
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
ROLE = ANSIBLE / "roles" / "alloy_collector"
errors = []

DEFAULT_DENY = "02f4bab031b57d1e30553ce08e0ec131"
SELF_OUT = "6125cb207f65033775d1069fdf6d0ccf"
# The only values iface and action may take as labels.
IFACE_ALLOWED = "vtnet[0-9]{1,2}|lo0|enc0|pflog0|igb[0-9]{1,2}|em[0-9]{1,2}"
ACTION_ALLOWED = "pass|block|match|rdr|nat|binat|scrub|rdr-anchor|nat-anchor"
RATELIMIT_LABEL = "add09fc915886757c300419fc2ecc1e6"
OTHER_LABEL = "0123456789abcdef0123456789abcdef"


def fail(msg):
    errors.append(msg)


def check(cond, msg):
    if not cond:
        fail(msg)


template = (ROLE / "templates" / "config.alloy.j2").read_text()
defaults = (ROLE / "defaults" / "main.yml").read_text()
start = template.index("{% if alloy_syslog_listener_port")
end = template.index("{% if alloy_pve_firewall_log")
section = template[start:end]


def central_drop_labels():
    text = (ANSIBLE / "inventory" / "group_vars" / "central" / "main.yml").read_text()
    m = re.search(r"^alloy_syslog_drop_filterlog_rules:\n((?:[ ]+- .*\n?)+)", text, re.M)
    return re.findall(r"^[ ]+- ([0-9a-f]{32})\b", m.group(1), re.M) if m else []


DROP_LABELS = central_drop_labels()


def wan_default():
    m = re.search(r"^alloy_syslog_drop_filterlog_iface:[ ]*(\S+)", defaults, re.M)
    return m.group(1) if m else None


# --- layer one: the template text --------------------------------------------
def test_text():
    check(wan_default() == "vtnet0", "alloy_syslog_drop_filterlog_iface does not default to vtnet0: " + str(wan_default()))

    check(DEFAULT_DENY in DROP_LABELS, "group_vars/central no longer drops the default deny " + DEFAULT_DENY)
    check(SELF_OUT not in DROP_LABELS,
          "group_vars/central drops " + SELF_OUT + ", the firewall's own outbound rule, whose line is the outbound "
          "leg of every tenant and lease flow and must reach Loki")

    listener = re.search(r'listener \{.*?labels\s*=\s*\{([^}]*)\}', section, re.S)
    check(listener is not None and re.search(r'\bjob\s*=\s*"firewall-other"', listener.group(1)),
          'the listener labels do not carry the static job = "firewall-other"')

    relabel = re.search(r'loki\.relabel "firewall_syslog" \{(.*?)\n\}\n', section, re.S)
    check(relabel is not None, "no loki.relabel firewall_syslog block")
    if relabel:
        rule = re.search(r'rule \{(.*?)\n  \}', relabel.group(1), re.S)
        body = rule.group(1) if rule else ""
        check('source_labels = ["__syslog_message_app_name"]' in body, "the relabel rule does not read the syslog app name")
        check('target_label  = "job"' in body, "the relabel rule does not write job")
        names = re.search(r'regex\s*=\s*"\^\(([^)]*)\)\$"', body)
        check(names is not None and sorted(names.group(1).split("|")) == ["filterlog", "pfstats", "suricata", "unbound"],
              "the relabel rule does not restrict job to suricata, filterlog, unbound and pfstats, anchored")

    process = section[section.index('loki.process "firewall_syslog"'):]
    expressions = re.findall(r"stage\.regex \{\s*expression = `([^`]*)`", process)
    check(len(expressions) == 2, "expected one stage.regex per field (iface, action), found " + str(len(expressions)))
    if len(expressions) == 2:
        check("(?P<iface>" + IFACE_ALLOWED + ")," in expressions[0] and expressions[0].count("(?P<") == 1,
              "the iface capture is not limited to " + IFACE_ALLOWED + ": " + expressions[0])
        check("(?P<action>" + ACTION_ALLOWED + ")," in expressions[1] and expressions[1].count("(?P<") == 1,
              "the action capture is not limited to " + ACTION_ALLOWED + ": " + expressions[1])
    labels_at = process.find("stage.labels")
    drop_selector = re.search(r'selector = `([^`]*)`\n\s*action\s*=\s*"drop"', process)
    check(drop_selector is not None, "no drop stage")
    if drop_selector:
        check('iface="{{ alloy_syslog_drop_filterlog_iface }}"' in drop_selector.group(1),
              "the drop selector does not select on iface through alloy_syslog_drop_filterlog_iface")
        check(labels_at != -1 and labels_at < process.index(drop_selector.group(0)),
              "the iface and action labels are not set before the drop stage")
    limit = re.search(r'selector = `([^`]*)`\n\n\s*stage\.limit', process)
    check(limit is not None, "no rate-limit stage")
    if limit:
        check("iface" not in limit.group(1), "the rate-limit selector is tied to an interface")
        check(labels_at != -1 and labels_at < process.index(limit.group(0)), "the labels are not set before the rate limit")


# --- layer two: Alloy, for real -------------------------------------------------
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def snappy_decode(data):
    """Snappy block format, which is what Loki's push API is compressed with."""
    pos, length, shift = 0, 0, 0
    while True:
        c = data[pos]
        pos += 1
        length |= (c & 0x7F) << shift
        if c < 0x80:
            break
        shift += 7
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            n = tag >> 2
            if n < 60:
                n += 1
            else:
                extra = n - 59
                n = int.from_bytes(data[pos:pos + extra], "little") + 1
                pos += extra
            out += data[pos:pos + n]
            pos += n
            continue
        if kind == 1:
            n = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | data[pos]
            pos += 1
        elif kind == 2:
            n = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
        else:
            n = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        for _ in range(n):
            out.append(out[-offset])
    return bytes(out)


def proto_fields(data):
    """(field number, wire type, value) for each field of a protobuf message."""
    pos = 0
    while pos < len(data):
        key, shift, pos = 0, 0, pos
        while True:
            c = data[pos]
            pos += 1
            key |= (c & 0x7F) << shift
            if c < 0x80:
                break
            shift += 7
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, shift = 0, 0
            while True:
                c = data[pos]
                pos += 1
                value |= (c & 0x7F) << shift
                if c < 0x80:
                    break
                shift += 7
        elif wire == 2:
            size, shift = 0, 0
            while True:
                c = data[pos]
                pos += 1
                size |= (c & 0x7F) << shift
                if c < 0x80:
                    break
                shift += 7
            value = data[pos:pos + size]
            pos += size
        elif wire == 1:
            value = data[pos:pos + 8]
            pos += 8
        elif wire == 5:
            value = data[pos:pos + 4]
            pos += 4
        else:
            raise ValueError("unsupported protobuf wire type " + str(wire))
        yield number, wire, value


def decode_push(body):
    """[(labels dict, line)] from a Loki PushRequest (snappy, protobuf)."""
    out = []
    for number, _, stream in proto_fields(snappy_decode(body)):
        if number != 1:
            continue
        labels, entries = "", []
        for n, _, v in proto_fields(stream):
            if n == 1:
                labels = v.decode()
            elif n == 2:
                for en, _, ev in proto_fields(v):
                    if en == 2:
                        entries.append(ev.decode())
        parsed = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', labels))
        out.extend((parsed, line) for line in entries)
    return out


class FakeLoki:
    def __init__(self):
        self.entries = []
        self.lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    got = decode_push(body)
                except Exception as e:  # a decoder bug must show up, not hide
                    got = [({"decode_error": str(e)}, "")]
                with outer.lock:
                    outer.entries.extend(got)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return list(self.entries)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def jinja_environment():
    try:
        import jinja2
    except ImportError:
        return None
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["bool"] = lambda x: x if isinstance(x, bool) else str(x).lower() in ("1", "true", "yes")
    return env


def alloy_binary():
    for candidate in ("/usr/bin/alloy", "/usr/local/bin/alloy"):
        if pathlib.Path(candidate).exists():
            return candidate
    return None


def render_section(env, syslog_port, drop_labels, ratelimit_labels):
    text = env.from_string(section).render(
        alloy_syslog_listener_port=syslog_port,
        alloy_syslog_drop_filterlog_rules=drop_labels,
        alloy_syslog_drop_filterlog_iface=wan_default(),
        alloy_syslog_ratelimit_filterlog_rules=ratelimit_labels,
        alloy_pve_firewall_log=False,
    )
    # Loopback only: the role listens on every address, a test must not.
    return text.replace("0.0.0.0:" + str(syslog_port), "127.0.0.1:" + str(syslog_port))


def frame(app, message):
    """One RFC 5424 line, newline framed, as a syslog sender writes it."""
    return ("<134>1 2026-11-09T12:00:00+00:00 opnsense " + app + " 4242 - - " + message + "\n").encode()


def filterlog(label, iface, action, tag):
    return frame("filterlog", "5,,," + label + "," + iface + ",match," + action
                 + ",in,4,0x0,,64,1,0,DF,6,tcp,60,192.0.2.1,198.51.100.1,40000,22,0,S,1,,64240,,mss," + tag)


def test_alloy():
    env = jinja_environment()
    binary = alloy_binary()
    if env is None or binary is None:
        print("SKIPPED the Alloy run: needs " + ("jinja2" if env is None else "") + (" and " if env is None and binary is None else "")
              + ("the alloy binary" if binary is None else ""))
        return
    syslog_port, http_port = free_port(), free_port()
    loki = FakeLoki()
    proc = None
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        try:
            config = render_section(env, syslog_port, DROP_LABELS, [RATELIMIT_LABEL])
            config += ('\nloki.write "central" {\n  endpoint {\n    url = "http://127.0.0.1:' + str(loki.port)
                       + '/loki/api/v1/push"\n  }\n}\n')
            (tmp / "config.alloy").write_text(config)

            v = subprocess.run([binary, "validate", str(tmp / "config.alloy")], capture_output=True, text=True)
            check(v.returncode == 0, "alloy validate failed: " + (v.stdout + v.stderr).strip()[-600:])
            f = subprocess.run([binary, "fmt", str(tmp / "config.alloy")], capture_output=True, text=True)
            check(f.returncode == 0, "alloy fmt failed: " + (f.stdout + f.stderr).strip()[-600:])
            if v.returncode != 0:
                return

            log = open(tmp / "alloy.log", "w")
            proc = subprocess.Popen(
                [binary, "run", "--server.http.listen-addr=127.0.0.1:" + str(http_port),
                 "--storage.path=" + str(tmp / "data"), "--disable-reporting", str(tmp / "config.alloy")],
                stdout=log, stderr=subprocess.STDOUT, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp)})
            conn, deadline = None, time.time() + 25
            while time.time() < deadline and conn is None:
                if proc.poll() is not None:
                    break
                try:
                    conn = socket.create_connection(("127.0.0.1", syslog_port), timeout=1)
                except OSError:
                    time.sleep(0.3)
            if conn is None:
                fail("Alloy never opened the syslog listener: " + (tmp / "alloy.log").read_text()[-600:])
                return

            frames = [
                filterlog(DEFAULT_DENY, "vtnet0", "block", "A-deny-wan"),
                filterlog(DEFAULT_DENY, "vtnet1", "block", "B-deny-lease"),
                filterlog(SELF_OUT, "vtnet0", "pass", "C-self-wan"),
                filterlog(SELF_OUT, "vtnet2", "pass", "D-self-tenant"),
                filterlog(OTHER_LABEL, "vtnet0", "block", "E-other-wan"),
                # Forged fields: anything but the fixed sets must not become a label.
                filterlog(OTHER_LABEL, "evil_iface_AAAA", "block", "F1-iface"),
                filterlog(OTHER_LABEL, "vtnet1", "evil_action_BBBB", "F2-action"),
                filterlog(OTHER_LABEL, "vtnet123", "pass", "F3-three-digits"),
                filterlog(OTHER_LABEL, "igb99999", "block", "F4-long-suffix"),
                filterlog(DEFAULT_DENY, "vtnet0evil", "block", "F5-forged-wan"),
                # Every allowed value.
                filterlog(OTHER_LABEL, "vtnet12", "rdr-anchor", "G1-allowed"),
                filterlog(OTHER_LABEL, "lo0", "nat", "G2-allowed"),
                filterlog(OTHER_LABEL, "enc0", "binat", "G3-allowed"),
                filterlog(OTHER_LABEL, "pflog0", "scrub", "G4-allowed"),
                filterlog(OTHER_LABEL, "igb3", "match", "G5-allowed"),
                filterlog(OTHER_LABEL, "em1", "nat-anchor", "G6-allowed"),
                frame("suricata", '{"event_type":"alert","tag":"S-suricata"}'),
                frame("unbound", "[1:0] info: 192.0.2.1 example.org. A IN tag=U-unbound"),
                frame("pfstats", "banned=0 tag=P-pfstats"),
                frame("attacker42", "tag=X-attacker"),
                frame("evil-filterlog", "tag=X-evil-filterlog"),
                frame("Suricata", "tag=X-case"),
                frame("filterlog2", "tag=X-suffix"),
            ]
            # A flood of the rate-limited rule, first on one interface and then on
            # another: one shared limit, not one per interface.
            frames += [filterlog(RATELIMIT_LABEL, "vtnet1", "block", "R1-" + str(i)) for i in range(40)]
            frames += [filterlog(RATELIMIT_LABEL, "vtnet0", "block", "R0-" + str(i)) for i in range(40)]
            # The syslog reader holds a message back until the next one starts, so
            # the sentinel that marks the end of the run is followed by a spare.
            frames.append(frame("pfstats", "banned=0 tag=Z-sentinel"))
            frames.append(frame("pfstats", "banned=0 tag=Y-spare"))
            conn.sendall(b"".join(frames))

            deadline = time.time() + 30
            while time.time() < deadline:
                if any("Z-sentinel" in line for _, line in loki.snapshot()):
                    break
                time.sleep(0.3)
            time.sleep(2)
            check_entries(loki.snapshot())
            conn.close()
        finally:
            if proc is not None:
                proc.kill()
                proc.wait()
            loki.close()


def check_entries(entries):
    errs = [lbl["decode_error"] for lbl, _ in entries if "decode_error" in lbl]
    check(not errs, "the fake Loki could not decode a push: " + str(errs[:1]))
    by_tag = {}
    for labels, line in entries:
        m = re.search(r"([A-Z][0-9]?-[\w-]+)\"?\}?$", line.strip())
        if m:
            by_tag[m.group(1)] = labels
    check(len(entries) > 5, "Alloy pushed only " + str(len(entries)) + " entries")

    def present(tag):
        return tag in by_tag

    check(not present("A-deny-wan"), "the default deny on the WAN was not dropped")
    check(present("B-deny-lease"), "the default deny on another interface was dropped, hiding what it catches there")
    check(present("C-self-wan"),
          "the firewall's own outbound rule on the WAN was dropped, losing the outbound leg of every tenant flow")
    check(present("D-self-tenant"), "the firewall's own outbound rule on another interface was dropped")
    check(present("E-other-wan"), "a rule that is not on the drop list was dropped")

    for tag, iface, action in (("B-deny-lease", "vtnet1", "block"), ("C-self-wan", "vtnet0", "pass"),
                               ("D-self-tenant", "vtnet2", "pass"), ("E-other-wan", "vtnet0", "block"),
                               ("G1-allowed", "vtnet12", "rdr-anchor"), ("G2-allowed", "lo0", "nat"),
                               ("G3-allowed", "enc0", "binat"), ("G4-allowed", "pflog0", "scrub"),
                               ("G5-allowed", "igb3", "match"), ("G6-allowed", "em1", "nat-anchor")):
        lbl = by_tag.get(tag, {})
        check(lbl.get("job") == "filterlog" and lbl.get("iface") == iface and lbl.get("action") == action,
              tag + " has labels " + str(lbl) + ", not job=filterlog iface=" + iface + " action=" + action)

    # Forged fields: the line is kept, and only the bad field has no label.
    for tag, iface, action in (("F1-iface", None, "block"), ("F2-action", "vtnet1", None),
                               ("F3-three-digits", None, "pass"), ("F4-long-suffix", None, "block"),
                               ("F5-forged-wan", None, "block")):
        lbl = by_tag.get(tag)
        check(lbl is not None, tag + " was not stored; a forged field must only lose its label, not the line")
        if lbl is not None:
            check(lbl.get("iface") == iface and lbl.get("action") == action,
                  tag + " has iface=" + str(lbl.get("iface")) + " action=" + str(lbl.get("action"))
                  + ", expected iface=" + str(iface) + " action=" + str(action))
    for tag, lbl in by_tag.items():
        check(re.fullmatch(IFACE_ALLOWED, lbl.get("iface", "lo0")) is not None,
              tag + " has the iface label " + str(lbl.get("iface")) + ", outside the allowed set")
        check(re.fullmatch(ACTION_ALLOWED, lbl.get("action", "pass")) is not None,
              tag + " has the action label " + str(lbl.get("action")) + ", outside the allowed set")
    for tag, job in (("S-suricata", "suricata"), ("U-unbound", "unbound"), ("P-pfstats", "pfstats"),
                     ("X-attacker", "firewall-other"), ("X-evil-filterlog", "firewall-other"),
                     ("X-case", "firewall-other"), ("X-suffix", "firewall-other")):
        check(by_tag.get(tag, {}).get("job") == job,
              tag + " was stored as job=" + str(by_tag.get(tag, {}).get("job")) + ", not " + job)
    for tag, lbl in by_tag.items():
        check(lbl.get("source") == "opnsense" and lbl.get("host") == "opnsense" and lbl.get("role") == "firewall",
              tag + " lost the listener's static labels: " + str(lbl))
        check(not any(k.startswith("__") for k in lbl), tag + " kept an internal label: " + str(lbl))

    flooded = [tag for tag in by_tag if tag.startswith("R")]
    check(15 <= len(flooded) <= 30,
          str(len(flooded)) + " of 80 flooded lines were kept, expected the burst of 20 plus a few (rate 5 a second)")
    check(any(t.startswith("R1-") for t in flooded) and all(by_tag[t].get("action") == "block" for t in flooded),
          "the rate limit did not apply to the first interface in the flood, or lost the action label")


def main():
    test_text()
    test_alloy()
    if errors:
        for e in errors:
            print("FAIL: " + e)
        return 1
    print("ok: the firewall syslog pipeline drops on the WAN only, bounds job, and rate limits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
