# Product landscape research

**Date:** 2026-10-27
**Purpose:** bounded comparison research for the Mailforge capability atlas.
**Evidence rule:** official product or vendor documentation only; this is
external research, not a statement that Mailforge implements these features.

## Findings

| Product | Officially documented capability emphasis | Product lesson for Mailforge |
| --- | --- | --- |
| Gammadyne Mailer | Desktop campaign sending, mail merge, scheduling, throttling, multiple SMTP servers, tracking, personalized attachments, incoming POP3/IMAP/direct-disk processing, list/database management, exclusions, bounce processing, scripting, and command-line automation. | The closest legacy comparator is a broad operator tool. A credible replacement needs explicit send, receive, list, automation, and operational-state boundaries rather than only a renderer. |
| Mautic | Email marketing, personalization and A/B testing, lead nurturing, campaign builders, landing pages/forms, web tracking, analytics, integrations, REST API, privacy/data sovereignty, and lead scoring. | Marketing automation and behavioral data are separate product domains. They should remain roadmap decisions, not be implied by local SMTP preparation. |
| ActiveCampaign | Email marketing and automation, segmentation, CRM/ecommerce integrations, conditional/predictive content, attribution, custom objects, APIs/webhooks, reporting, A/B testing, inbox preview, and multi-step automation. | Commercial parity includes workflow, reporting, integrations, and testing surfaces beyond message delivery. |
| Brevo | Official help documents reusable segments for contact management, campaigns, and automations, consent groups, campaign tagging, and separate campaign/transactional/automation channels. | Consent groups, dynamic segments, and channel separation are explicit policy concepts worth preserving in future product decisions. |

## Interpretation

The competitor set confirms that “mailer” is not one capability. It is a
portfolio spanning contact identity, consent and segmentation, content
composition, scheduling, provider execution, inbound feedback, reporting, and
automation. Mailforge has meaningful local foundations in identity-adjacent
sorting, durable synthetic state, safe composition, and evidence-oriented
inbound processing. It does not yet have provider execution, inbox access,
behavioral tracking, a workflow builder, a dashboard, or a production service
boundary.

The comparison is directional. It does not establish feature equivalence,
license compatibility, pricing, deliverability, or suitability for a specific
operator or jurisdiction.

## Official sources

- [Gammadyne feature list](https://www.gammadyne.com/gm-features.htm)
- [Gammadyne email automation](https://www.gammadyne.com/gm-email_automation.htm)
- [Gammadyne incoming mail](https://www.gammadyne.com/gm-incoming.htm)
- [Gammadyne database integration](https://www.gammadyne.com/gm-database.htm)
- [Gammadyne sending features](https://www.gammadyne.com/gm-sending.htm)
- [Mautic features](https://mautic.org/features/)
- [ActiveCampaign feature and pricing matrix](https://www.activecampaign.com/pricing?stream=top)
- [Brevo segments](https://help.brevo.com/hc/en-us/articles/8423408941330-Use-segments-contact-management-email-campaigns-automations)
- [Brevo consent groups](https://help.brevo.com/hc/en-us/articles/33595674951314-Create-and-manage-consent-groups)
- [Brevo channel distinctions](https://help.brevo.com/hc/en-us/articles/360021196220-What-are-the-differences-between-marketing-transactional-and-automation-emails)
