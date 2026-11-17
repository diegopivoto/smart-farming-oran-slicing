#!/bin/bash
S=$(cd "$(dirname "$0")" && pwd)
$S/e3.sh drone 16 || exit $?
TAG=-weakpos $S/e1.sh drone 5
