#!/usr/bin/env python3
"""Update vendor configuration while preserving unrelated settings.

Called by the explicit --apply wrappers. Arguments are paths, never secrets.
"""
import os
from pathlib import Path
import re
import sys
import tempfile
import xml.etree.ElementTree as ET


def atomic_write(path, content):
    path = Path(path)
    previous = path.stat()
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".security-config-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), previous.st_mode & 0o777)
            os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_password(path):
    value = Path(path).read_text().removesuffix("\n")
    if len(value) < 20 or any(c.isspace() for c in value):
        raise ValueError("enrollment password must be at least 20 characters with no whitespace")


def configure_wazuh(path):
    # ossec.conf can contain multiple top-level ossec_config elements.
    original = re.sub(r"<\?xml[^?]*\?>", "", Path(path).read_text())
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring("<configuration>" + original + "</configuration>", parser=parser)
    configs = root.findall("ossec_config")
    if not configs:
        raise ValueError("ossec.conf contains no ossec_config element")
    auths = root.findall("ossec_config/auth")
    if not auths:
        auths = [ET.SubElement(configs[0], "auth")]
    for auth in auths:
        for name, value in {"disabled": "no", "remote_enrollment": "yes",
                            "use_password": "yes",
                            "ssl_manager_cert": "etc/sslmanager.cert",
                            "ssl_manager_key": "etc/sslmanager.key"}.items():
            nodes = auth.findall(name)
            if not nodes:
                nodes = [ET.SubElement(auth, name)]
            for node in nodes:
                node.text = value
    ET.indent(root)
    atomic_write(path, "\n".join(ET.tostring(child, encoding="unicode") for child in root) + "\n")


def configure_crowdsec(config_path, credentials_path, url):
    import yaml
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("CROWDSEC_LAPI_URL must be an HTTPS URL without credentials")
    config = yaml.safe_load(Path(config_path).read_text())
    credentials = yaml.safe_load(Path(credentials_path).read_text())
    server = config.setdefault("api", {}).setdefault("server", {})
    server["listen_uri"] = "0.0.0.0:8080"
    server.setdefault("tls", {}).update(cert_file="/etc/crowdsec/tls/server.crt",
                                         key_file="/etc/crowdsec/tls/server.key")
    credentials["url"] = url
    credentials["ca_cert_path"] = "/etc/crowdsec/tls/ca.crt"
    config["api"].setdefault("client", {}).pop("insecure_skip_verify", None)
    credentials.pop("insecure_skip_verify", None)
    atomic_write(config_path, yaml.safe_dump(config, sort_keys=False))
    atomic_write(credentials_path, yaml.safe_dump(credentials, sort_keys=False))


if __name__ == "__main__":
    {"check-password": check_password, "wazuh": configure_wazuh,
     "crowdsec": configure_crowdsec}[sys.argv[1]](*sys.argv[2:])
