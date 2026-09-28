#!/bin/sh
set -eu
mkdir -p "$FIXTURE_ROOT"
{
    echo "MESSAGE=$MESSAGE"
    echo "LOUD=$LOUD"
    echo "EXTRA_WORDS=$EXTRA_WORDS"
    echo "_REMOTE_USER=$_REMOTE_USER"
    echo "FIXTURE_ENV=$FIXTURE_ENV"
} > "$FIXTURE_ROOT/feature"
