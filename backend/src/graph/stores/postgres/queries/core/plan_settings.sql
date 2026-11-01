-- Session settings of an exact vector query, local to the transaction it
-- runs in (not a template). Parameter: plan_cache_mode (text).
SELECT set_config('plan_cache_mode', :plan_cache_mode, TRUE) AS plan_cache_mode
