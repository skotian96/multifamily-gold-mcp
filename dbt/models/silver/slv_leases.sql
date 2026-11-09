-- One row per lease, typed and keyed to the canonical unit_id.
--
-- Lease dates stay separate (CLAUDE.md rule 2): lease_start/lease_end are the
-- contract; move_in/move_out are physical events; notice_date is when notice
-- was given. Renewals legitimately have null move_in/move_out.
--
-- Back-dated move-outs: staff sometimes record a move-out weeks after it
-- happened. We TRUST THE EVENT DATE: move_out is the date the resident left,
-- and every downstream occupancy calculation must use it. The entry date is
-- kept only for audit (move_out_entered_on, move_out_entry_lag_days,
-- is_backdated_move_out) and must never be used to place the event in time.
with src as (
    select
        trim(l.lease_id)                          as lease_id,
        trim(l.resident_id)                       as resident_id,
        trim(l.property_id)                       as property_id,
        x.unit_id,
        lower(trim(l.lease_type))                 as lease_type,
        cast(l.lease_start as date)               as lease_start,
        cast(l.lease_end as date)                 as lease_end,
        cast(l.move_in as date)                   as move_in,
        cast(l.notice_date as date)               as notice_date,
        cast(l.move_out as date)                  as move_out,
        cast(l.move_out_entered_on as date)       as move_out_entered_on,
        nullif(trim(l.move_out_reason), '')       as move_out_reason,
        cast(l.monthly_rent as decimal(10, 2))    as monthly_rent
    from {{ ref('brz_leases') }} as l
    left join {{ ref('slv_unit_crosswalk') }} as x
        on  x.property_id   = trim(l.property_id)
        and x.raw_unit_code = l.unit_code
)

select
    *,
    date_diff('day', move_out, move_out_entered_on)            as move_out_entry_lag_days,
    coalesce(date_diff('day', move_out, move_out_entered_on) >= 14, false)
                                                               as is_backdated_move_out
from src
