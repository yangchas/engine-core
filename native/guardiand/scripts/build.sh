#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p bin
go build -buildvcs=false -o bin/guardiand .
