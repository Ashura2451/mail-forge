# Mailforge 033 qualification record

## Verified locally

- Schema 25 initializes and migration is repeatable.
- A controlled proof is created only from a published Message Studio revision.
- The proof stores the exact MIME digest and binds sender and recipient.
- Provider grants and submission reservations bind the proof hash and reject
  mismatched caller content before transport contact.
- Campaign preparation stores proof metadata while continuing to own MIME in
  `prepared_messages`.
- Proof rows survive normal SQLite reopen and remain separate from ambiguous
  provider outcomes.

Evidence: `tests/test_canonical_outbound_proof_033.py` and the focused Message
Studio/local-application regression run on 2026-10-30.

## Not verified in this milestone

Real-provider acceptance, inbox delivery, consent or legal policy approval,
provider quotas, production-scale performance, Windows behavior, signed or
notarized packaging, and universal client rendering remain out of scope. The
browser acceptance exercises the local proof-bound form; it does not establish
provider or inbox qualification.
