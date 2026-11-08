#!/bin/sh
# Local certificates for Postgres over verified TLS: `make postgres-tls-certs` runs this script, and so does
# `make up WITH=tls` (docs/configuration.md "PostgreSQL over TLS").
#
#   sh scripts/postgres-tls.sh DIR
#
# Writes a throwaway CA and a server certificate signed by it into DIR. ca.pem goes into the app containers at
# /etc/specflow/postgres/rds-ca-bundle.pem, the path where an Aurora deployment mounts the AWS RDS bundle;
# server.pem and server.key go to the postgres service. The server certificate names specflow-postgres (the
# compose network), localhost and 127.0.0.1 (the host tools), so PGSSLMODE=verify-full checks the host name as
# it does on Aurora. A complete set is kept: delete DIR to get new certificates. Local only: DIR is git-ignored.
# Works with OpenSSL and with the LibreSSL of macOS.
set -eu

dir=${1:?usage: sh scripts/postgres-tls.sh DIR}
if [ -f "$dir/ca.pem" ] && [ -f "$dir/server.pem" ] && [ -f "$dir/server.key" ]; then
	echo "postgres-tls: certificates exist in $dir (delete it to get new ones)"
	exit 0
fi

umask 077
mkdir -p "$dir"
conf="$dir/openssl.cnf"
cat >"$conf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = specflow-postgres
[ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[server]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
subjectAltName = DNS:specflow-postgres, DNS:localhost, IP:127.0.0.1
EOF

openssl req -x509 -new -newkey rsa:2048 -nodes -days 825 -config "$conf" -extensions ca \
	-subj "/CN=Specflow local Postgres CA" -keyout "$dir/ca.key" -out "$dir/ca.pem"
openssl req -new -newkey rsa:2048 -nodes -config "$conf" -keyout "$dir/server.key" -out "$dir/server.csr"
openssl x509 -req -days 825 -in "$dir/server.csr" -CA "$dir/ca.pem" -CAkey "$dir/ca.key" -CAcreateserial \
	-extfile "$conf" -extensions server -out "$dir/server.pem"
rm -f "$conf" "$dir/server.csr" "$dir/ca.srl"
# The app containers run as a non-root user and read the CA through a bind mount.
chmod 644 "$dir/ca.pem" "$dir/server.pem"
echo "postgres-tls: wrote ca.pem, server.pem and server.key to $dir"
