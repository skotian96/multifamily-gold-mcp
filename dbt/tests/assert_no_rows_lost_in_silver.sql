-- Silver may restructure rows but must not lose them: every raw lease and
-- ledger line survives, and every raw guest card is merged into exactly one
-- prospect.
with checks as (
    select 'leases' as entity,
           (select count(*) from {{ ref('brz_leases') }})  as bronze_rows,
           (select count(*) from {{ ref('slv_leases') }})  as silver_rows
    union all
    select 'charges',
           (select count(*) from {{ ref('brz_charges') }}),
           (select count(*) from {{ ref('slv_charges') }})
    union all
    select 'guest_cards',
           (select count(*) from {{ ref('brz_guest_cards') }}),
           (select sum(card_count) from {{ ref('slv_guest_cards') }})
)
select * from checks where bronze_rows <> silver_rows
