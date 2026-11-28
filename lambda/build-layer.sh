#!/usr/bin/env bash
# Builds anthropic-layer.zip: the Anthropic Python SDK packaged as a Lambda layer
# (python3.12, x86_64). Run from anywhere with python3 + pip installed:
#   bash lambda/build-layer.sh
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build anthropic-layer.zip
PIP_BREAK_SYSTEM_PACKAGES=1 pip3 install -q -r requirements.txt --target build/python \
  --platform manylinux2014_x86_64 --python-version 3.12 \
  --implementation cp --only-binary=:all:
# boto3/botocore are already in the Lambda runtime but the SDK may need a newer one: keep them.
find build -name "__pycache__" -type d -prune -exec rm -rf {} +
(cd build && zip -qr ../anthropic-layer.zip python)
rm -rf build
ls -lh anthropic-layer.zip
