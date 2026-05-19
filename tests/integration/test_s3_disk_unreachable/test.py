"""Tests that the `access_check_retry_attempts` per-disk config caps the S3 retry
budget during `IDisk::checkAccess` at startup.

Without the cap, an unreachable S3 endpoint would force the AWS SDK to exhaust
its default 500-attempt budget, stalling startup for tens of minutes. With a low
cap, the access check fails within seconds and the server gives up cleanly.
"""

import re

import pytest

from helpers.cluster import ClickHouseCluster

NODE_NAME = "node"

# A localhost port that nothing listens on — connection refused is instant,
# unlike an unresolvable hostname which blocks on DNS timeout. Must be outside
# the CI port pool range (30000-50000) defined in `helpers/cluster.py`.
UNREACHABLE_PORT = 60112

# Cap small enough that even with exponential backoff the access check
# completes in under a few seconds. Without the cap the default budget of
# 500 attempts would take roughly 40 minutes to exhaust.
RETRY_CAP = 2

DISK_CONFIG = f"""<clickhouse>
    <storage_configuration>
        <disks>
            <unreachable_s3_disk>
                <type>s3</type>
                <endpoint>http://localhost:{UNREACHABLE_PORT}/bucket/key/</endpoint>
                <access_key_id>minio</access_key_id>
                <secret_access_key>ClickHouse_Minio_P@ssw0rd</secret_access_key>
                <access_check_retry_attempts>{RETRY_CAP}</access_check_retry_attempts>
                <retry_initial_delay_ms>1</retry_initial_delay_ms>
                <retry_max_delay_ms>10</retry_max_delay_ms>
                <connect_timeout_ms>500</connect_timeout_ms>
                <request_timeout_ms>1000</request_timeout_ms>
            </unreachable_s3_disk>
        </disks>
    </storage_configuration>
</clickhouse>
"""

CONFIG_PATH = "/etc/clickhouse-server/config.d/unreachable_s3.xml"


@pytest.fixture(scope="module")
def cluster():
    try:
        cluster = ClickHouseCluster(__file__)
        cluster.add_instance(NODE_NAME, stay_alive=True)
        cluster.start()
        yield cluster
    finally:
        cluster.shutdown()


def test_access_check_retry_cap_fails_fast(cluster):
    """With a small `access_check_retry_attempts`, an unreachable S3 disk
    aborts startup quickly instead of exhausting the default retry budget."""
    node = cluster.instances[NODE_NAME]

    node.replace_config(CONFIG_PATH, DISK_CONFIG)
    node.stop_clickhouse()

    # 60 seconds is plenty when the cap is honored (a couple of attempts plus
    # tiny backoff). Without the cap, the SDK's default 500 attempts would
    # blow well past this window.
    try:
        node.start_clickhouse(start_wait_sec=60, expected_to_fail=True)

        # The per-attempt warning we emit is "Attempt N/M failed with retryable
        # error: ...". With cap = RETRY_CAP, M is RETRY_CAP + 1. Verify the SDK
        # never exceeded the configured cap.
        log = node.grep_in_log(
            "Attempt .* failed with retryable error", from_host=True
        )
        attempts = [
            int(m.group(1))
            for m in re.finditer(r"Attempt (\d+)/(\d+) failed", log)
        ]
        assert attempts, "expected at least one retry-attempt warning in the log"
        assert max(attempts) <= RETRY_CAP + 1, (
            f"retry budget was not capped: saw attempt {max(attempts)} "
            f"but cap was {RETRY_CAP} (expected max {RETRY_CAP + 1})"
        )
    finally:
        # Restore a clean config so other tests in the module (or teardown)
        # do not inherit a server that refuses to start.
        node.exec_in_container(["bash", "-c", f"rm -f {CONFIG_PATH}"])
        node.start_clickhouse(start_wait_sec=30)
        assert node.query("SELECT 1").strip() == "1"
