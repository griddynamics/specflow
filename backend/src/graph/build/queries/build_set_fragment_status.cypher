// Persist: set the status and error code of fragments in a batch.
UNWIND $rows AS row
MATCH (f:Fragment {group_id: $project_id, fragment_id: row.fragment_id})
SET f.status = row.status, f.error_code = row.error_code, f.last_build_id = $build_id
RETURN count(f) AS written
