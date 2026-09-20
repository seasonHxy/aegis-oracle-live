use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::{collections::HashSet, path::Path};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub feed: String,
    #[serde(default)]
    pub simulation: bool,
    pub interval_ms: u64,
    pub max_age_secs: u64,
    pub min_sources: usize,
    pub min_groups: usize,
    pub max_spread_bps: u32,
    pub outlier_band_bps: u32,
    pub mad_multiplier: u32,
    pub max_confidence_bps: u32,
    pub max_jump_bps: u32,
    pub sources: Vec<Source>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Source {
    pub name: String,
    pub group: String,
    #[serde(default = "one")]
    pub weight: u32,
    #[serde(flatten)]
    pub adapter: Adapter,
}
fn one() -> u32 {
    1
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Adapter {
    Coinbase,
    Kraken,
    Bitstamp,
    Static {
        price: String,
    },
    /// JSON pointer paths. Source timestamp is mandatory and expressed in milliseconds.
    Json {
        url: String,
        price_path: String,
        timestamp_path: String,
        #[serde(default)]
        bid_path: Option<String>,
        #[serde(default)]
        ask_path: Option<String>,
    },
}
impl Config {
    pub fn read(path: &Path) -> Result<Self> {
        let c: Self = serde_json::from_slice(&std::fs::read(path)?).context("invalid config")?;
        c.validate()?;
        Ok(c)
    }
    pub fn validate(&self) -> Result<()> {
        ensure!(!self.feed.trim().is_empty(), "feed is empty");
        ensure!(
            (500..=60000).contains(&self.interval_ms),
            "interval must be 500..60000 ms"
        );
        ensure!(
            (5..=86400).contains(&self.max_age_secs),
            "max_age_secs must be 5..86400"
        );
        ensure!(
            self.interval_ms < self.max_age_secs * 1000,
            "interval must be less than max age"
        );
        ensure!(
            self.min_sources > 0 && self.min_sources <= self.sources.len(),
            "invalid source quorum"
        );
        let groups: HashSet<_> = self.sources.iter().map(|s| &s.group).collect();
        ensure!(
            self.min_groups > 0 && self.min_groups <= groups.len(),
            "invalid group quorum"
        );
        ensure!(
            self.max_confidence_bps <= 10000 && self.max_jump_bps <= 10000 && self.max_jump_bps > 0,
            "invalid risk limits"
        );
        ensure!(
            self.mad_multiplier > 0 && self.outlier_band_bps > 0,
            "invalid outlier settings"
        );
        let mut names = HashSet::new();
        for s in &self.sources {
            ensure!(
                !s.name.is_empty() && !s.group.is_empty() && s.weight > 0 && s.weight <= 1000,
                "invalid source metadata"
            );
            ensure!(names.insert(&s.name), "duplicate source name");
            match &s.adapter {
                Adapter::Coinbase | Adapter::Kraken | Adapter::Bitstamp => {
                    ensure!(
                        matches!(self.feed.as_str(), "ETH/USD" | "BTC/USD"),
                        "native adapters support ETH/USD and BTC/USD only"
                    );
                    let venue = s.adapter.venue().unwrap();
                    ensure!(
                        s.group == venue,
                        "native adapter group must match its venue"
                    );
                    ensure!(
                        self.interval_ms >= 10000,
                        "native REST adapters require interval_ms >= 10000"
                    );
                }
                Adapter::Static { price } => {
                    crate::aggregation::parse_price(price)?;
                }
                Adapter::Json {
                    url,
                    price_path,
                    timestamp_path,
                    bid_path,
                    ask_path,
                } => {
                    let u = reqwest::Url::parse(url)?;
                    ensure!(
                        u.username().is_empty() && u.password().is_none(),
                        "URL credentials are not supported"
                    );
                    if u.scheme() != "https"
                        && !(u.scheme() == "http"
                            && matches!(u.host_str(), Some("localhost" | "127.0.0.1" | "[::1]")))
                    {
                        bail!("source URL must use HTTPS or loopback HTTP");
                    }
                    ensure!(
                        price_path.starts_with('/') && timestamp_path.starts_with('/'),
                        "JSON pointers required"
                    );
                    ensure!(
                        bid_path.is_some() == ask_path.is_some(),
                        "bid and ask must be supplied together"
                    );
                }
            }
        }
        Ok(())
    }
    pub fn simulated(&self) -> bool {
        self.simulation
            || self
                .sources
                .iter()
                .any(|s| matches!(s.adapter, Adapter::Static { .. }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Config {
        serde_json::from_str(include_str!("../../config/demo.json")).unwrap()
    }
    #[test]
    fn demo_valid_and_marked() {
        let c = config();
        c.validate().unwrap();
        assert!(c.simulated());
    }
    #[test]
    fn duplicate_names_invalid() {
        let mut c = config();
        c.sources[1].name = c.sources[0].name.clone();
        assert!(c.validate().is_err());
    }
    #[test]
    fn zero_quorum_invalid() {
        let mut c = config();
        c.min_groups = 0;
        assert!(c.validate().is_err());
    }
    #[test]
    fn insecure_url_invalid() {
        let mut c = config();
        c.sources[0].adapter = Adapter::Json {
            url: "http://example.com/price".into(),
            price_path: "/price".into(),
            timestamp_path: "/timestamp".into(),
            bid_path: None,
            ask_path: None,
        };
        assert!(c.validate().is_err());
    }
    #[test]
    fn timestamp_required() {
        let mut c = config();
        c.sources[0].adapter = Adapter::Json {
            url: "https://example.com/price".into(),
            price_path: "/price".into(),
            timestamp_path: "".into(),
            bid_path: None,
            ask_path: None,
        };
        assert!(c.validate().is_err());
    }
}

impl Adapter {
    pub fn venue(&self) -> Option<&'static str> {
        match self {
            Self::Coinbase => Some("coinbase"),
            Self::Kraken => Some("kraken"),
            Self::Bitstamp => Some("bitstamp"),
            _ => None,
        }
    }
}

#[cfg(test)]
mod native_tests {
    use super::*;
    fn live() -> Config {
        serde_json::from_str(include_str!("../../config/live-eth-usd.json")).unwrap()
    }
    #[test]
    fn live_config_is_not_simulation() {
        let c = live();
        c.validate().unwrap();
        assert!(!c.simulated());
    }
    #[test]
    fn cannot_fake_independent_venue() {
        let mut c = live();
        c.sources[0].group = "another-venue".into();
        assert!(c.validate().is_err());
    }
    #[test]
    fn cannot_mix_quote_currency() {
        let mut c = live();
        c.feed = "ETH/USDT".into();
        assert!(c.validate().is_err());
    }
    #[test]
    fn prevents_fast_polling() {
        let mut c = live();
        c.interval_ms = 500;
        assert!(c.validate().is_err());
    }
}
