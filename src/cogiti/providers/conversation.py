"""Commands whose whole effect is the reply.

A greeting has no provider work to do, and giving it one would be pretending.
It exists so the table can still route it: an intent with no entry escalates,
and paying a model to answer "thanks" is exactly the cost the fast path exists
to avoid.
"""

from . import Result, provider


@provider("conversation.acknowledge")
def acknowledge(**_args):
    return Result(values={})
