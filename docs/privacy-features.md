# Privacy and hosted-feature controls

Patch `1007-personal-feature-disable-hosted-services-telemetry-and-promotions.patch`
keeps hosted services and telemetry present in the code but inactive. It gates
cloud-dependent and promotional surfaces so jcode runs without contacting
hosted services unless a provider is explicitly allowed.

## Telemetry inactive

Telemetry code remains present but is forced off. No telemetry is collected,
shown, or sent, including when `JCODE_NO_TELEMETRY=0` is set.

## Hosted subscription and account surfaces

Hosted account and subscription paths remain in the code but are hidden and
rejected:

- Account, subscription, billing, upgrade, and marketing surfaces are hidden
  and rejected.
- The jcode hosted provider is denied unless listed in
  `allowed_providers`, and is never the default or automatically selected.
- Jev continues to work with the user's own providers and never routes through
  the jcode gateway.

## Sponsored discovery inactive

The `compile_remote` and sponsored integration tools are not registered, and
sponsors are forced off.

## Automatic onboarding inactive

Automatic onboarding never starts. Manual onboarding rehearsal remains
available, and login import requires an explicit Continue.

## Direct endpoint routing

Jev uses the user's own providers, never the jcode gateway. Jade cloud sessions
and the Jade relay are hidden, rejected, and never started. The `/remote` cloud
sales link is hidden, while local `/remote`, `/cloud`, and `/local` remain
available.
