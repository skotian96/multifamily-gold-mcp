-- Physical events must be in a sensible order, and a move-out can't be keyed
-- in before it happened (back-dating only ever goes one way).
select lease_id, move_in, notice_date, move_out, move_out_entered_on
from {{ ref('slv_leases') }}
where move_out < move_in
   or notice_date > move_out
   or move_out_entered_on < move_out
   or lease_end < lease_start
