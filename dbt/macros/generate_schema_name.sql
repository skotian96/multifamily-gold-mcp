{#
  Use the configured schema name as-is (bronze / silver / gold) instead of
  dbt's default "<target_schema>_<custom_schema>" (e.g. main_silver).
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
