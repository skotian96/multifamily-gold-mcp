-- One row per deduplicated prospect (a person enquiring about one property).
--
-- MATCHING RULE: two guest cards are the same person when
--   1. they are for the same property, AND
--   2. they share a contact key: phone digits (punctuation stripped), or
--      lower-cased email if the phone is blank, AND
--   3. the full name is close to the name on the EARLIEST card for that
--      contact key: Jaro-Winkler similarity >= 0.85 on lower-cased
--      "first last". This tolerates one-letter typos ("Edwrd" vs "Edward")
--      but keeps genuinely different people who share a phone (e.g. a
--      household) apart.
-- Cards that fail rule 3 stand alone as their own prospect.
--
-- SURVIVORSHIP:
--   prospect_id, first_contact_date, original_source -> earliest card
--     (first-touch attribution: the source that actually generated the lead).
--   name, desired move-in, desired unit type -> most-progressed card
--     (Leased > Denied > Cancelled > Toured > Lost > Prospect), since a leased
--     card's name matches the resident record.
--   tour/application dates -> earliest non-null; resident_id -> any non-null.
with cards as (
    select
        trim(guest_card_id)                                    as guest_card_id,
        trim(property_id)                                      as property_id,
        trim(first_name)                                       as first_name,
        trim(last_name)                                        as last_name,
        nullif(lower(trim(email)), '')                         as email,
        nullif(regexp_replace(phone, '[^0-9]', '', 'g'), '')   as phone_digits,
        trim(source)                                           as source,
        cast(first_contact_date as date)                       as first_contact_date,
        cast(desired_move_in as date)                          as desired_move_in,
        trim(desired_unit_type)                                as desired_unit_type,
        cast(tour_date as date)                                as tour_date,
        cast(application_date as date)                         as application_date,
        trim(status)                                           as status,
        nullif(trim(resident_id), '')                          as resident_id,
        lower(trim(first_name) || ' ' || trim(last_name))      as name_key,
        case trim(status)
            when 'Leased'    then 1
            when 'Denied'    then 2
            when 'Cancelled' then 3
            when 'Toured'    then 4
            when 'Lost'      then 5
            else 6
        end                                                    as status_rank
    from {{ ref('brz_guest_cards') }}
),

anchored as (
    select
        *,
        first_value(guest_card_id) over contact_group as anchor_card_id,
        first_value(name_key)      over contact_group as anchor_name_key
    from cards
    window contact_group as (
        partition by property_id, coalesce(phone_digits, email, guest_card_id)
        order by first_contact_date, guest_card_id
    )
),

matched as (
    select
        *,
        case
            when jaro_winkler_similarity(name_key, anchor_name_key) >= 0.85
                then anchor_card_id
            else guest_card_id
        end as prospect_id
    from anchored
),

ranked as (
    select
        *,
        row_number() over (partition by prospect_id
                           order by first_contact_date, guest_card_id)        as first_touch_rank,
        row_number() over (partition by prospect_id
                           order by status_rank, first_contact_date, guest_card_id) as progress_rank
    from matched
),

rollup as (
    select
        prospect_id,
        count(*)                                              as card_count,
        string_agg(guest_card_id, ',' order by guest_card_id) as source_guest_card_ids,
        min(tour_date)                                        as tour_date,
        min(application_date)                                 as application_date,
        max(resident_id)                                      as resident_id,
        max(email)                                            as any_email
    from matched
    group by prospect_id
)

select
    r.prospect_id,
    ft.property_id,
    best.first_name,
    best.last_name,
    coalesce(best.email, r.any_email)    as email,
    ft.phone_digits                      as phone,
    ft.source                            as original_source,
    ft.first_contact_date,
    best.desired_move_in,
    best.desired_unit_type,
    r.tour_date,
    r.application_date,
    best.status,
    r.resident_id,
    r.card_count,
    r.card_count > 1                     as had_duplicates,
    r.source_guest_card_ids
from rollup as r
join ranked as ft   on ft.prospect_id = r.prospect_id   and ft.first_touch_rank = 1
join ranked as best on best.prospect_id = r.prospect_id and best.progress_rank = 1
