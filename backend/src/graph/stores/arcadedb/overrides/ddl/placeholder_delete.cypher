// The placeholder spec node of one domain label (__LABEL__), deleted once its type exists.
MATCH (n:SpecNode:__LABEL__ {group_id: $group, uuid: $uuid})
DETACH DELETE n
