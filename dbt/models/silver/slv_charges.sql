-- One row per resident-ledger transaction.
--
-- Unit codes are resolved through slv_unit_crosswalk. Charge codes are mapped
-- to a category via slv_charge_codes; codes missing from the chart are KEPT
-- with charge_category = 'unmapped' (never dropped, so the ledger still
-- balances) and flagged by a warn-level test until someone maps them.
-- Sign convention: charges positive, concessions and payments negative.
select
    trim(c.transaction_id)                     as transaction_id,
    trim(c.property_id)                        as property_id,
    x.unit_id,
    trim(c.resident_id)                        as resident_id,
    trim(c.lease_id)                           as lease_id,
    cast(c.transaction_date as date)           as transaction_date,
    cast(c.post_month as date)                 as post_month,
    upper(trim(c.charge_code))                 as charge_code,
    coalesce(cc.charge_category, 'unmapped')   as charge_category,
    cc.charge_code is not null                 as is_mapped_code,
    trim(c.description)                        as description,
    cast(c.amount as decimal(12, 2))           as amount
from {{ ref('brz_charges') }} as c
left join {{ ref('slv_unit_crosswalk') }} as x
    on  x.property_id   = trim(c.property_id)
    and x.raw_unit_code = c.unit_code
left join {{ ref('slv_charge_codes') }} as cc
    on  cc.charge_code = upper(trim(c.charge_code))
