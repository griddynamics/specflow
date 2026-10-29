// Wait until the indexes are online or the timeout passes (unscoped).
CALL db.awaitIndexes($timeout_s)
