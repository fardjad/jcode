# Privacy and cleanup features

Patch `1006-personal-feature-remove-hosted-services-and-telemetry.patch`
removes hosted subscription, telemetry, promotional, sponsored-discovery,
and onboarding functionality from jcode. It is a personal patch that strips
cloud-dependent and promotional surfaces so jcode runs as a local-only tool.

## Telemetry removal

Disables all telemetry delivery at its shared boundary and removes telemetry
CLI commands and controls. No telemetry events are collected or sent.

## Hosted subscription removal

Removes hosted subscription and account functionality:

- Removes standalone hosted subscription commands, sales pitches, rate-limit
  upsells, and promotional UI surfaces.
- Removes hosted subscription promotions, sales pitches, and upgrade nudges
  from onboarding flows.
- Removes hosted account management and device authorization from the CLI.
- Removes hosted account status, billing, and management commands from the
  TUI.
- Removes unused hosted account, billing, and activation API code.
- Removes obsolete hosted account, billing, and cloud documentation.

## Sponsored discovery removal

Removes remote sponsored integration discovery and partner tracking, along
with obsolete sponsored discovery and attribution documentation.

## Automatic onboarding removal

Makes local jcode sessions start without an automatic onboarding flow.

## Direct endpoint routing

Routes the jcode provider choice through the generic OpenAI-compatible profile
instead of the managed subscription wrapper, so configured endpoints and API
keys are the only credentials used.
