#!/bin/bash
# Start SCAD2STEP on Linux. Everything stays inside this folder.
exec bash "$(dirname "$0")/app/portable.sh" "$@"
