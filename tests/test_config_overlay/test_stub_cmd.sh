#!/bin/sh
# Hermetic test stub replacing every shell_command and every command_line
# command that would call pstryk_engine.py / solarman_v5_client.py /
# deye_cloud_client.py inside the Tier 2 sandbox. Never touches the network.
#   $1 = name of the stubbed command (for logs only)
#   exit code = contents of /config/.test_stub_rc (default 0)
echo '{}'
rc=$(cat /config/.test_stub_rc 2>/dev/null || echo 0)
exit "${rc:-0}"
