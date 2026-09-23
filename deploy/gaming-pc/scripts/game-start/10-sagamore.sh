#!/usr/bin/env bash
# ES-DE runs every script in this directory on game launch, and WAITS for them.
# The worker detaches its own slow work and always exits 0.
exec "$HOME/.local/bin/sagamore-esde" start "$@"
