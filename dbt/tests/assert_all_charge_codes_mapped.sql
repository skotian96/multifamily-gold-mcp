-- WARN, don't fail: unknown charge codes are kept as 'unmapped' so the ledger
-- balances, but someone must add them to charge_codes before gold reports
-- on fees. Each returned row is a ledger line awaiting a mapping.
{{ config(severity = 'warn') }}

select transaction_id, property_id, charge_code, amount
from {{ ref('slv_charges') }}
where charge_category = 'unmapped'
