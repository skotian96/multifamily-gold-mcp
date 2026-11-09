-- Non-revenue units (model, employee, down, office) must not carry leases.
select l.lease_id, l.unit_id, u.unit_use
from {{ ref('slv_leases') }} as l
join {{ ref('slv_units') }} as u using (unit_id)
where not u.is_rentable
