-- One row per unit, keyed by the canonical unit_id '<property_id>-<unit number>'
-- (e.g. P300-300). The unit master is the authority for which units exist.
with src as (
    select
        trim(property_id) as property_id,
        cast(regexp_extract(trim(unit_code), '(\d+)\s*$', 1) as integer) as unit_number,
        *  exclude (property_id)
    from {{ ref('brz_units') }}
)

select
    property_id || '-' || unit_number                as unit_id,
    property_id,
    unit_number,
    'STE-' || unit_number                            as unit_code,
    trim(unit_type)                                  as unit_type,
    cast(bedrooms as integer)                        as bedrooms,
    cast(bathrooms as decimal(3, 1))                 as bathrooms,
    cast(sqft as integer)                            as sqft,
    cast(market_rent as decimal(10, 2))              as current_market_rent,
    lower(trim(unit_use))                            as unit_use,
    -- Non-revenue units (model, employee, down, office) are not rentable and
    -- must never enter occupancy or GPR denominators (CLAUDE.md rule 1).
    case upper(trim(exclude_from_occupancy))
        when 'N' then true
        when 'Y' then false
    end                                              as is_rentable
from src
