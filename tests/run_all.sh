#!/bin/bash
# Run every test. Needs a running X session and a mounted Google Drive with an
# exchange folder - the copy test creates and removes a __pytest__ folder in it.

set -u
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

failed=0
for test in test_copy_engine.py test_escape.py test_alignment.py; do
    echo "════════════════════════════════════════════════════════════"
    echo "  $test"
    echo "════════════════════════════════════════════════════════════"
    if python3 "$test"; then
        echo
    else
        failed=$((failed + 1))
        echo "  ^^ $test FAILED"
        echo
    fi
done

if (( failed )); then
    echo "$failed test file(s) failed."
    exit 1
fi
echo "All test files passed."
