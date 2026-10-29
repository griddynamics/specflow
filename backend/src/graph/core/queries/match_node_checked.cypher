// Fragment: MATCH one node of the project by $<var>_key and check its domain label; joined into upsert_relationship.
// __TAIL__ is a slot rendered empty: its RETURN only makes the fragment a complete statement.
MATCH (__VAR__:__LABEL__ {group_id: $project_id, __KEY__: $__VAR___key})
WHERE $__VAR___label IN labels(__VAR__)/*__TAIL__*/
RETURN __VAR__.__KEY__ AS key/*__TAIL__*/
