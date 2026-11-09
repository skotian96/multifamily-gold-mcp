-- One row per owner.
select
    trim(owner_id)   as owner_id,
    trim(owner_name) as owner_name
from {{ ref('brz_owners') }}
