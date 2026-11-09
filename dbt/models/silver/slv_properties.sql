-- One row per property. owner_id is renamed current_owner_id because the raw
-- column only reflects ownership on the extract date (see slv_property_owners).
select
    trim(property_id)            as property_id,
    trim(property_name)          as property_name,
    trim(city)                   as city,
    upper(trim(state))           as state,
    cast(unit_count as integer)  as unit_count,
    trim(owner_id)               as current_owner_id
from {{ ref('brz_properties') }}
