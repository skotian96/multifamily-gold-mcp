-- One row per valid ledger code, with its standard category
-- (rent, concession, fee, payment).
select
    upper(trim(charge_code))   as charge_code,
    trim(description)          as charge_code_description,
    lower(trim(category))      as charge_category
from {{ ref('brz_charge_codes') }}
