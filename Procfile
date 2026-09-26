# Railway / Heroku-style start command.
#
# --workers 1 is deliberate: the rate limiter and the period-view cache live in
# process memory, so each extra worker would enforce its own separate limit and
# keep its own separate cache. One worker keeps both correct. Scale by moving
# that state to Redis first, not by adding workers.
web: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
