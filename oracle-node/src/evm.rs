use crate::{aggregation::Aggregate, config::Config, state::Store};
use anyhow::{ensure, Context, Result};
use ethers::{
    abi::{encode, Abi, Token},
    prelude::*,
    utils::keccak256,
};
use std::{sync::Arc, time::Duration};
pub type ReportTuple = ([u8; 32], u128, u64, u64, u64, u32);
pub struct Evm {
    pub chain_id: u64,
    pub address: Address,
    pub client: Arc<SignerMiddleware<Provider<Http>, LocalWallet>>,
    pub contract: Contract<SignerMiddleware<Provider<Http>, LocalWallet>>,
}
impl Evm {
    pub async fn connect(rpc: &str, address: &str, c: &Config) -> Result<Self> {
        let http = reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(10))
            .build()?;
        let provider = Provider::new(Http::new_with_client(reqwest::Url::parse(rpc)?, http))
            .interval(Duration::from_millis(250));
        let chain_id = provider.get_chainid().await?.as_u64();
        ensure!(
            [31337, 11155111, 84532].contains(&chain_id),
            "MVP supports Anvil, Sepolia and Base Sepolia only"
        );
        ensure!(
            chain_id == 31337 || !c.simulated(),
            "static fixtures are only permitted on local chain 31337"
        );
        let key = std::env::var("ORACLE_PRIVATE_KEY")
            .context("ORACLE_PRIVATE_KEY is required for publishing")?;
        let wallet = key
            .parse::<LocalWallet>()
            .context("invalid oracle key")?
            .with_chain_id(chain_id);
        let client = Arc::new(SignerMiddleware::new(provider, wallet));
        let address: Address = address.parse()?;
        ensure!(
            !client.get_code(address, None).await?.is_empty(),
            "no contract at address"
        );
        let abi: Abi = serde_json::from_str(include_str!("../abi/AegisOracle.json"))?;
        let contract = Contract::new(address, abi, client.clone());
        let signer: Address = contract.method("signer", ())?.call().await?;
        ensure!(
            signer == client.address(),
            "configured wallet is not the authorized oracle signer"
        );
        let (max_age, max_confidence, enabled): (u64, u32, bool) = contract
            .method("feeds", keccak256(c.feed.as_bytes()))?
            .call()
            .await?;
        ensure!(
            enabled && c.max_age_secs <= max_age && c.max_confidence_bps <= max_confidence,
            "local feed limits incompatible with on-chain configuration"
        );
        Ok(Self {
            chain_id,
            address,
            client,
            contract,
        })
    }
    pub async fn latest(&self, feed: &str) -> Result<ReportTuple> {
        Ok(self
            .contract
            .method("latestReport", keccak256(feed.as_bytes()))?
            .call()
            .await?)
    }
    pub async fn checked_price(&self, feed: &str) -> Result<(u128, u64, u64)> {
        Ok(self
            .contract
            .method("latestPrice", keccak256(feed.as_bytes()))?
            .call()
            .await?)
    }
    pub async fn recover(&self, store: &mut Store) -> Result<Option<String>> {
        let Some(raw) = store.state.pending_raw.clone() else {
            return Ok(None);
        };
        let raw: Bytes = raw.parse()?;
        let hash = H256::from(keccak256(raw.as_ref()));
        ensure!(
            Some(format!("{hash:#x}")) == store.state.pending_hash,
            "transaction journal hash mismatch"
        );
        let mut receipt = self.client.get_transaction_receipt(hash).await?;
        if receipt.is_none() {
            // If the node forgot the transaction, rebroadcast the SAME signed bytes, never create a new nonce.
            if self.client.get_transaction(hash).await?.is_none() {
                let _pending = self
                    .client
                    .provider()
                    .send_raw_transaction(raw)
                    .await
                    .context("rebroadcast pending transaction failed; journal retained")?;
            }
            for _ in 0..20 {
                tokio::time::sleep(Duration::from_millis(250)).await;
                receipt = self.client.get_transaction_receipt(hash).await?;
                if receipt.is_some() {
                    break;
                }
            }
        }
        let receipt = receipt
            .context("transaction still pending; no new report will be sent until resolved")?;
        // Require two confirmations outside Anvil; journal survives reorgs during this window.
        let confirmations = if self.chain_id == 31337 { 1 } else { 2 };
        let block = receipt
            .block_number
            .context("receipt has no block number")?
            .as_u64();
        ensure!(
            self.client.get_block_number().await?.as_u64() >= block + confirmations - 1,
            "waiting for transaction confirmations"
        );
        store.state.pending_hash = None;
        store.state.pending_raw = None;
        store.save()?;
        ensure!(
            receipt.status == Some(U64::from(1)),
            "oracle transaction reverted"
        );
        Ok(Some(format!("{hash:#x}")))
    }
    pub async fn publish(&self, c: &Config, a: &Aggregate, store: &mut Store) -> Result<String> {
        ensure!(
            store.state.pending_raw.is_none(),
            "unresolved pending transaction"
        );
        let previous = self.latest(&c.feed).await?;
        let sequence = previous.4.checked_add(1).context("sequence exhausted")?;
        let report: ReportTuple = (
            keccak256(c.feed.as_bytes()),
            a.price,
            a.observed_at,
            a.observed_at + c.max_age_secs,
            sequence,
            a.confidence_bps,
        );
        let digest = report_digest(self.chain_id, self.address, report);
        let signature = self.client.signer().sign_hash(digest)?;
        let call = self
            .contract
            .method::<_, ()>("submit", (report, Bytes::from(signature.to_vec())))?;
        let mut tx = call.tx;
        self.client.fill_transaction(&mut tx, None).await?;
        let sig = self.client.signer().sign_transaction(&tx).await?;
        let raw = tx.rlp_signed(&sig);
        let hash = H256::from(keccak256(raw.as_ref()));
        store.state.pending_raw = Some(format!("{raw:#x}"));
        store.state.pending_hash = Some(format!("{hash:#x}"));
        store.save()?;
        // recover broadcasts from the durable journal and confirms the transaction.
        self.recover(store)
            .await?
            .context("missing publication receipt")
    }
}
pub fn report_digest(chain: u64, address: Address, r: ReportTuple) -> H256 {
    let domain=keccak256(encode(&[
        Token::FixedBytes(keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)").to_vec()),
        Token::FixedBytes(keccak256("AegisOracle").to_vec()),Token::FixedBytes(keccak256("1").to_vec()),Token::Uint(chain.into()),Token::Address(address)]));
    let body=keccak256(encode(&[
        Token::FixedBytes(keccak256("Report(bytes32 feedId,uint128 price,uint64 observedAt,uint64 validUntil,uint64 sequence,uint32 confidenceBps)").to_vec()),
        Token::FixedBytes(r.0.to_vec()),Token::Uint(r.1.into()),Token::Uint(r.2.into()),Token::Uint(r.3.into()),Token::Uint(r.4.into()),Token::Uint(r.5.into())]));
    H256::from(keccak256([&[0x19, 0x01][..], &domain, &body].concat()))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn domain_separation() {
        let r = ([1; 32], 100, 1, 60, 1, 10);
        assert_ne!(
            report_digest(1, Address::zero(), r),
            report_digest(2, Address::zero(), r)
        );
        assert_ne!(
            report_digest(1, Address::zero(), r),
            report_digest(1, Address::from_low_u64_be(1), r)
        );
    }
}
