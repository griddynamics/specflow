// ArcadeDB override of build/queries/build_merge_spec_nodes.cypher, step 1 of 3: the survivor gets its
// final spec_id_aliases; written counts the survivors found (the strict row check of persist).
UNWIND $rows AS row
MATCH (new:SpecNode {group_id: $project_id, uuid: row.survivor})
SET new.spec_id_aliases = row.aliases, new.last_build_id = $build_id
RETURN count(new) AS written
