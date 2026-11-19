"""The cards of Alex's working day other than the billing incident, one module each.

Every card module has the same shape:

``NAME``                         the card's key in the day's state and parameters
``generate(seed, ctx)``          seeded parameters (``ctx`` holds the billing close and the cards generated before it)
``initial(params)``              the card's state at the start of the shift
``note(params)``, ``card(...)``  Riley's handoff note and the card on the board
``POLICY``                       the handbook entries for it
``TOOLS``                        Alex's own tools: name -> (handler(day, args, now), description, schema, unchecked)
``VIEWS``                        what Alex can read: name -> (view(day, now), description)
``TOPICS``                       what colleagues can be asked: name -> who holds it, what it is, how the keyword reader spots it
``reply(day, topic, actor, items, now)``  what a colleague says and does when asked
``turn(day, actor, now)``        optional: what a colleague does whenever a message reaches them
``tick(day, t)``                 optional: the card's seeded events at time ``t``
``result(day)``                  how the card ended
"""
from . import approvals, audit, disputes, erasure, payout, pricing, recon, rotation, window

CARDS = (rotation, disputes, pricing, approvals, recon, erasure, window, payout, audit)
