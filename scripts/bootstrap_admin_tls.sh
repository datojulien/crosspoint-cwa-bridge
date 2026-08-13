#!/bin/sh
set -eu

state_dir=${1:-${ADMIN_STATE_HOST_DIR:-./state}}
bridge_ip=${2:-${BRIDGE_ADDRESS:-}}
if test -z "$bridge_ip"; then
  echo "Usage: $0 [state-directory] BRIDGE_IP" >&2
  echo "Example: $0 ./state 192.168.1.50" >&2
  exit 2
fi
certificate=$state_dir/tls.crt
private_key=$state_dir/tls.key

umask 077
if test -L "$state_dir"; then
  echo "The administration state directory must not be a symbolic link." >&2
  exit 1
fi
install -d -m 700 "$state_dir"

for state_file in "$certificate" "$private_key"; do
  if test -L "$state_file" || { test -e "$state_file" && ! test -f "$state_file"; }; then
    echo "TLS state files must be regular files, not links or special files." >&2
    exit 1
  fi
done

if { test -e "$certificate" && ! test -e "$private_key"; } || \
   { test -e "$private_key" && ! test -e "$certificate"; }; then
  echo "TLS state is incomplete; refusing to overwrite either file." >&2
  exit 1
fi

if ! test -e "$certificate"; then
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
    -keyout "$private_key" \
    -out "$certificate" \
    -subj "/CN=$bridge_ip/O=CrossPoint CWA Bridge" \
    -addext "subjectAltName=IP:$bridge_ip" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"
fi

chmod 600 "$private_key"
chmod 644 "$certificate"
openssl x509 -in "$certificate" -noout -subject -dates -fingerprint -sha256
