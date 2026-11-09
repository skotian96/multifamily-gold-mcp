{#
  Load one raw CSV exactly as delivered.

  Every column is read as VARCHAR (all_varchar) so no value is coerced,
  trimmed or reformatted; typing and cleaning happen in silver.
  Two lineage columns are appended:
    _loaded_at   - when this bronze table was built
    _source_file - the file each row came from
  Raw folder defaults to ../data/raw (relative to dbt/); override with MFG_RAW_DIR.
#}
{% macro bronze_csv(file_name) %}
{%- set raw_dir = env_var('MFG_RAW_DIR', '../data/raw') -%}
select
    * exclude (filename),
    current_timestamp as _loaded_at,
    filename as _source_file
from read_csv(
    '{{ raw_dir }}/{{ file_name }}.csv',
    header = true,
    all_varchar = true,
    filename = true
)
{% endmacro %}
