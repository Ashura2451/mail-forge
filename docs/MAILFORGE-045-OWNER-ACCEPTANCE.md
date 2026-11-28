# MAILFORGE-045 · Owner acceptance guide

Use an isolated application-data directory and synthetic recipients. Existing
external authorization is exhausted; this guide authorizes no additional
Mailtrap or other external SMTP submission.

1. In Sort email, import a structured CSV containing valid, duplicate,
   malformed, known-provider, custom/unknown, and associated fields. Inspect
   counts and the separate visible-domain and optional mail-host classifications.
2. Select one provider, several providers, and all providers in turn. Confirm
   that export preserves email, source position, and associated fields, and that
   the selected result transfers directly to Compare and Send without manual
   reconstruction. Exercise union, intersection, subtraction, duplicates, and
   accepted-history exclusion in Compare.
3. In Send, use the transferred structured result. Add plain text,
   personalized HTML, an HTML file with an asset bundle when applicable, and a
   file attachment. Review the personalized preview and confirm the attachment
   count and exact recipient scope.
4. Use “Send distinct test email” with one explicitly entered synthetic
   recipient. Confirm it reports a separate one-recipient operation and does
   not start the prepared audience send.
5. With a capture-only loopback provider, authorize and run the prepared send.
   Inspect captured MIME independently: recipients, personalized HTML, plain
   text, attachment names/content, and embedded assets. Confirm history and
   per-recipient outcomes, then repeat the existing restart/no-duplicate check.
6. Review “Use an external sending account” only when a fresh Business Owner
   authorization exists. Confirm provider, exact recipients, expiry, and bounded
   submission budget before execution. The panel itself must not connect.
7. Review the retained external evidence: alpha Mailtrap receipt is confirmed
   by the Business Owner screenshot; beta is retained as one permanent
   data-transmission failure with no retry. Do not retry beta or infer inbox
   placement, production capacity, or a precise provider rejection reason.

## Limits

The capture-only SMTP path proves bounded interoperability and MIME fidelity,
not inbox delivery. The Mailtrap sandbox result proves one independently
confirmed controlled receipt and one recorded failure, not production-scale
capacity or deliverability. The candidate remains unsigned and unnotarized.
Complaint ingestion is not an active operator capability; suppression,
unsubscribe, eligibility, and delivery-outcome authorities remain active.
