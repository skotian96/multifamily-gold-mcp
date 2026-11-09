-- Unit-code crosswalk: one row per (property_id, raw unit_code spelling) seen in
-- ANY bronze file, mapped to the canonical unit_id from slv_units.
--
-- Raw files spell the same unit as 'STE-300', 'Suite 300' or '300'. The unit
-- number is the trailing digits; together with property_id it identifies the
-- unit. Spellings that don't resolve to a real unit keep unit_id = null, which
-- fails the not_null / relationships tests instead of silently dropping rows.
-- All other silver models join through this table, never re-parse codes.
with spellings as (
    select property_id, unit_code, 'units' as source_table from {{ ref('brz_units') }}
    union all
    select property_id, unit_code, 'unit_market_rents' from {{ ref('brz_unit_market_rents') }}
    union all
    select property_id, unit_code, 'leases' from {{ ref('brz_leases') }}
    union all
    select property_id, unit_code, 'charges' from {{ ref('brz_charges') }}
),

distinct_spellings as (
    select
        trim(property_id)                                       as property_id,
        unit_code                                               as raw_unit_code,
        string_agg(distinct source_table, ', ' order by source_table) as seen_in,
        count(*)                                                as raw_row_count
    from spellings
    group by 1, 2
),

parsed as (
    select
        *,
        try_cast(regexp_extract(trim(raw_unit_code), '(\d+)\s*$', 1) as integer) as unit_number,
        case
            when trim(raw_unit_code) like 'STE-%'   then 'STE-###'
            when trim(raw_unit_code) like 'Suite %' then 'Suite ###'
            when regexp_full_match(trim(raw_unit_code), '\d+') then 'bare ###'
            else 'other'
        end as spelling_style
    from distinct_spellings
)

select
    p.property_id,
    p.raw_unit_code,
    u.unit_id,
    p.unit_number,
    p.spelling_style,
    p.seen_in,
    p.raw_row_count
from parsed as p
left join {{ ref('slv_units') }} as u
    on  u.property_id = p.property_id
    and u.unit_number = p.unit_number
