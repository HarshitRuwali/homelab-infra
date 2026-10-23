#!/usr/bin/env bash
# Configure an existing LAPI with verified TLS. Dry run by default.
source "$(dirname "$0")/_common.sh"
require_root; banner
CROWDSEC_TLS_CERT_FILE="${CROWDSEC_TLS_CERT_FILE:-/root/security-bootstrap/crowdsec.crt}"
CROWDSEC_TLS_KEY_FILE="${CROWDSEC_TLS_KEY_FILE:-/root/security-bootstrap/crowdsec.key}"
CROWDSEC_TLS_CA_FILE="${CROWDSEC_TLS_CA_FILE:-/root/security-bootstrap/ca.crt}"
CROWDSEC_LAPI_URL="${CROWDSEC_LAPI_URL:-https://sec-crowdsec:8080}"
[[ "$CROWDSEC_LAPI_URL" == https://* ]] || die "CROWDSEC_LAPI_URL must use HTTPS"
for input in "$CROWDSEC_TLS_CERT_FILE" "$CROWDSEC_TLS_KEY_FILE" "$CROWDSEC_TLS_CA_FILE"; do
  require_input_file "$input"
done
validate_tls_pair "$CROWDSEC_TLS_CERT_FILE" "$CROWDSEC_TLS_KEY_FILE"
if (( APPLY )); then
  openssl verify -CAfile "$CROWDSEC_TLS_CA_FILE" "$CROWDSEC_TLS_CERT_FILE"
fi
run systemctl stop crowdsec
run install -d -m 0700 /etc/crowdsec/tls
run install -m 0600 "$CROWDSEC_TLS_CERT_FILE" /etc/crowdsec/tls/server.crt
run install -m 0600 "$CROWDSEC_TLS_KEY_FILE" /etc/crowdsec/tls/server.key
run install -m 0644 "$CROWDSEC_TLS_CA_FILE" /etc/crowdsec/tls/ca.crt
run python3 "$(dirname "$0")/_configure.py" crowdsec /etc/crowdsec/config.yaml \
  /etc/crowdsec/local_api_credentials.yaml "$CROWDSEC_LAPI_URL"
run crowdsec -t
run systemctl enable --now crowdsec
run cscli lapi status
ok "CrowdSec LAPI uses HTTPS; clients must trust the CA and use the certificate's DNS name"
