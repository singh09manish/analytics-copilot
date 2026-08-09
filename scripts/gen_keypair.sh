#!/usr/bin/env bash
set -euo pipefail
mkdir -p secrets
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out secrets/copilot_svc_key.p8 -nocrypt
chmod 600 secrets/copilot_svc_key.p8
openssl rsa -in secrets/copilot_svc_key.p8 -pubout -out secrets/copilot_svc_key.pub
echo "--- paste this value into bootstrap.sql RSA_PUBLIC_KEY ---"
grep -v "PUBLIC KEY" secrets/copilot_svc_key.pub | tr -d '\n'; echo
