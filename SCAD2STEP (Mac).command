#!/bin/bash
# Double-click to start SCAD2STEP on macOS. Everything stays inside this folder.
# First time only: right-click → Open (or System Settings → Privacy & Security → Open Anyway).
exec bash "$(dirname "$0")/app/portable.sh" "$@"
