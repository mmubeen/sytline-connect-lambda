#!/usr/bin/env bash
# Generate this run's CA and the SQL Server certificate (SAN = the compose host name `sqlserver`) into mock/.run/.
# The CA PEM is stored in the mock credentials secret as SQL_CA_PEM, so the function's verified-TLS path is exercised.
# Nothing here is a real credential; the files are git-ignored and removed by `make -C mock clean`.
set -euo pipefail
out="${1:?usage: certs.sh <output-dir>}"
mkdir -p "$out"
cd "$out"
[[ -f ca.pem && -f sql.crt && -f sql.key ]] && exit 0
umask 077
openssl genrsa -out ca.key 2048 2>/dev/null
openssl req -x509 -new -key ca.key -sha256 -days 30 -subj "/CN=sytline-mock-ca" -out ca.pem
openssl genrsa -out sql.key 2048 2>/dev/null
openssl req -new -key sql.key -subj "/CN=sqlserver" -out sql.csr
printf 'subjectAltName=DNS:sqlserver,DNS:sqlserver-real,DNS:localhost\nextendedKeyUsage=serverAuth\n' > san.cnf
openssl x509 -req -in sql.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 30 -sha256 -extfile san.cnf -out sql.crt 2>/dev/null
rm -f sql.csr san.cnf ca.srl
chmod 644 ca.pem sql.crt sql.key   # the container's mssql user must read them; the directory is git-ignored
