#!/usr/bin/env bash
# Compile proto/messages.proto → proto/messages_pb2.py
# Uses grpcio-tools (NOT apt protoc — apt version is proto2-era)
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m grpc_tools.protoc \
  --proto_path=proto \
  --python_out=proto \
  proto/messages.proto
echo "Compiled: proto/messages_pb2.py"
