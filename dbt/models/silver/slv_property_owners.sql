-- One row per (property, ownership period). effective_to is inclusive;
-- null means the period is still open (current owner).
select
    trim(property_id)                as property_id,
    trim(owner_id)                   as owner_id,
    cast(effective_from as date)     as effective_from,
    cast(effective_to as date)       as effective_to,
    effective_to is null             as is_current
from {{ ref('brz_property_owners') }}
