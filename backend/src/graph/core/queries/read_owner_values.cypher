// Inheritance (data-model.md "Attributes"): for each request, the spec nodes one hop away from the node
// (by uuid) over the request's inherit relation, with their labels and the stored values of the
// request's attributes as [name, value] pairs. The reader keeps the owner-pair targets.
UNWIND $requests AS request
MATCH (n:SpecNode {group_id: $project_id, uuid: request.uuid})-[r]->(o:SpecNode)
WHERE type(r) = request.relation AND r.group_id = $project_id AND o.group_id = $project_id AND o.spec_id IS NOT NULL
RETURN request.uuid AS uuid, request.relation AS relation, labels(o) AS labels, o.spec_id AS spec_id, [name IN request.attributes | [name, o[name]]] AS attribute_values
ORDER BY uuid, relation, spec_id
