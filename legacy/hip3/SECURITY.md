# Security model

## Fail-closed guarantees in this repository

- Invalid, stale, future, non-positive, wide-spread, duplicate and unconfigured quotes are rejected.
- Quorum is checked both before and after outlier filtering.
- Source count and independent upstream group count are separate checks.
- Excessive confidence or price jumps block the complete multi-asset update.
- Partial HIP-3 payloads are refused.
- Payload maps are sorted before the official SDK serializes and signs them.
- State corruption stops startup instead of silently discarding the circuit-breaker reference.
- Live testnet publishing requires `dryRun=false`, `--live` and an explicit environment acknowledgement; mainnet adds another acknowledgement.
- Private keys are never accepted in the JSON configuration.

## Threats not solved by software alone

- Several providers may license or relay the same underlying market data.
- A fund NAV, reserve attestation or corporate action can be false at its origin.
- The updater host, process environment or hot key can be compromised.
- A valid but economically wrong contract specification may still expose the HIP-3 deployer to losses or slashing.
- Network partitions can leave the publisher unable to update while the market continues trading.
- A legitimate price gap can trigger a persistent circuit breaker and require an authorized human decision.

## Production requirements

Before mainnet, add at minimum:

1. Contractually authorized, independently sourced market data with documented redistribution rights.
2. Two or more publishers in active/passive mode with fencing so they cannot race the 2.5-second limit.
3. HSM/KMS or isolated signing service for the updater key; keep the deployer key cold.
4. External monitoring for source freshness, group quorum, confidence, divergence, API acceptance and onchain state.
5. Alerting and a separately authorized `haltTrading` runbook for market closure and oracle incidents.
6. Recorded raw observations and deterministic replay for every published price.
7. Testnet chaos tests for source manipulation, partial outage, clock skew, replay, API rejection and restart recovery.
8. Independent security review of configuration, source mappings and operating procedures.

Never include a private key, access token or paid market-data response in an issue or log bundle.
