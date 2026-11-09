-- One row per (unit, market-rent period). effective_to is inclusive; null = current.
with typed as (
    select
        x.unit_id,
        cast(r.effective_date as date)        as effective_from,
        cast(r.market_rent as decimal(10, 2)) as market_rent
    from {{ ref('brz_unit_market_rents') }} as r
    left join {{ ref('slv_unit_crosswalk') }} as x
        on  x.property_id   = trim(r.property_id)
        and x.raw_unit_code = r.unit_code
)

select
    unit_id,
    effective_from,
    lead(effective_from) over (partition by unit_id order by effective_from)
        - interval 1 day                                   as effective_to,
    market_rent
from typed
