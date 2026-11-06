"""The platform's database: users, credentials, and run metadata.

Plain SQL over psycopg 3, deliberately. There is no ORM here because the two
statements that matter most in this package -- the run state-transition guard
and the session-lease insert -- are each a single query whose exact shape IS the
safety argument. An ORM would hide both behind a method call.
"""
