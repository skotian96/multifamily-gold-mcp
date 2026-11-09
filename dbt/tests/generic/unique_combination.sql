{#
  Fails when the given columns, taken together, are not unique.
  Used for composite primary keys (avoids a dbt_utils dependency).
#}
{% test unique_combination(model, columns) %}
select
    {{ columns | join(', ') }},
    count(*) as row_count
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
