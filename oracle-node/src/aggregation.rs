use crate::config::Config;
use anyhow::{ensure, Result};
use rust_decimal::{prelude::ToPrimitive, Decimal};
use serde::Serialize;
use std::{
    collections::{BTreeMap, HashSet},
    str::FromStr,
};

pub const SCALE: u128 = 100_000_000;
// Bound ensures intermediate arithmetic cannot overflow u128, even with weights and bps.
pub const MAX_PRICE: u128 = 1_000_000_000_000_000_000;
pub fn parse_price(s: &str) -> Result<u128> {
    let d = Decimal::from_str(s)?;
    ensure!(d > Decimal::ZERO, "price must be positive");
    let scaled = d
        .checked_mul(Decimal::from(SCALE))
        .ok_or_else(|| anyhow::anyhow!("price overflow"))?;
    ensure!(scaled.fract().is_zero(), "price exceeds 8 decimals");
    let p = scaled
        .to_u128()
        .ok_or_else(|| anyhow::anyhow!("invalid price"))?;
    ensure!(p <= MAX_PRICE, "price exceeds supported range");
    Ok(p)
}
pub fn display_price(p: u128) -> String {
    format!("{}.{:08}", p / SCALE, p % SCALE)
}
#[derive(Debug, Clone, Serialize)]
pub struct Quote {
    pub source: String,
    pub group: String,
    pub weight: u32,
    #[serde(serialize_with = "price_string")]
    pub price: u128,
    pub observed_at_ms: u64,
    pub received_at_ms: u64,
    pub latency_ms: u64,
    pub bid: Option<u128>,
    pub ask: Option<u128>,
}
fn price_string<S: serde::Serializer>(p: &u128, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&display_price(*p))
}
#[derive(Debug, Clone, Serialize)]
pub struct Aggregate {
    #[serde(serialize_with = "price_string")]
    pub price: u128,
    pub observed_at: u64,
    pub confidence_bps: u32,
    pub source_count: usize,
    pub group_count: usize,
    pub accepted: Vec<String>,
    pub rejected: Vec<String>,
}
fn quantile(values: &[(u128, u32)], numerator: u128, denominator: u128) -> u128 {
    let mut v = values.to_vec();
    v.sort_by_key(|x| x.0);
    let total: u128 = v.iter().map(|x| x.1 as u128).sum();
    let mut acc = 0;
    for (p, w) in v {
        acc += w as u128;
        if acc * denominator >= total * numerator {
            return p;
        }
    }
    unreachable!()
}
fn quorum(c: &Config, q: &[Quote]) -> Result<()> {
    ensure!(
        q.len() >= c.min_sources,
        "insufficient valid sources: {} < {}",
        q.len(),
        c.min_sources
    );
    let groups: HashSet<_> = q.iter().map(|q| &q.group).collect();
    ensure!(
        groups.len() >= c.min_groups,
        "insufficient independent groups: {} < {}",
        groups.len(),
        c.min_groups
    );
    Ok(())
}
pub fn aggregate(
    c: &Config,
    quotes: Vec<Quote>,
    now_ms: u64,
    previous: Option<u128>,
) -> Result<Aggregate> {
    let mut accepted = vec![];
    let mut rejected = vec![];
    let mut seen = HashSet::new();
    for q in quotes {
        let configured = c.sources.iter().find(|s| s.name == q.source);
        let valid_meta = configured
            .map(|s| s.group == q.group && s.weight == q.weight)
            .unwrap_or(false);
        let spread_ok = match (q.bid, q.ask) {
            (None, None) => true,
            (Some(b), Some(a)) if b > 0 && b <= a && a <= MAX_PRICE => {
                (a - b) * 20000 <= (a + b) * c.max_spread_bps as u128
            }
            _ => false,
        };
        if !valid_meta
            || !seen.insert(q.source.clone())
            || q.price == 0
            || q.price > MAX_PRICE
            || q.observed_at_ms > now_ms
            || now_ms.saturating_sub(q.observed_at_ms) > c.max_age_secs * 1000
            || !spread_ok
        {
            rejected.push(format!(
                "{}: invalid, duplicate, stale, future or excessive spread",
                q.source
            ));
        } else {
            accepted.push(q);
        }
    }
    quorum(c, &accepted)?;
    let mut grouped: BTreeMap<String, Vec<&Quote>> = BTreeMap::new();
    for q in &accepted {
        grouped.entry(q.group.clone()).or_default().push(q);
    }
    let groups: Vec<_> = grouped
        .iter()
        .map(|(name, members)| {
            let v: Vec<_> = members.iter().map(|q| (q.price, q.weight)).collect();
            (
                name.clone(),
                quantile(&v, 1, 2),
                members.iter().map(|q| q.weight).max().unwrap(),
            )
        })
        .collect();
    let v: Vec<_> = groups.iter().map(|(_, p, w)| (*p, *w)).collect();
    let center = quantile(&v, 1, 2);
    let deviations: Vec<_> = v.iter().map(|(p, w)| (p.abs_diff(center), *w)).collect();
    let band = (quantile(&deviations, 1, 2) * c.mad_multiplier as u128)
        .max(center * c.outlier_band_bps as u128 / 10000);
    let retained: HashSet<_> = groups
        .iter()
        .filter(|(_, p, _)| p.abs_diff(center) <= band)
        .map(|(g, _, _)| g.clone())
        .collect();
    accepted.retain(|q| {
        if retained.contains(&q.group) {
            true
        } else {
            rejected.push(format!("{}: MAD outlier group", q.source));
            false
        }
    });
    quorum(c, &accepted)?;
    let v: Vec<_> = groups
        .iter()
        .filter(|(g, _, _)| retained.contains(g))
        .map(|(_, p, w)| (*p, *w))
        .collect();
    let price = quantile(&v, 1, 2);
    let deviation = price
        .abs_diff(quantile(&v, 1, 4))
        .max(price.abs_diff(quantile(&v, 3, 4)));
    let bps = (deviation * 10000).div_ceil(price);
    ensure!(
        bps <= c.max_confidence_bps as u128,
        "confidence exceeds limit"
    );
    if let Some(p) = previous {
        ensure!(p > 0 && p <= MAX_PRICE, "invalid reference price");
        ensure!(
            price.abs_diff(p) * 10000 <= p * c.max_jump_bps as u128,
            "price jump circuit breaker"
        );
    }
    Ok(Aggregate {
        price,
        observed_at: accepted
            .iter()
            .map(|q| q.observed_at_ms / 1000)
            .min()
            .unwrap(),
        confidence_bps: bps as u32,
        source_count: accepted.len(),
        group_count: retained.len(),
        accepted: accepted.iter().map(|q| q.source.clone()).collect(),
        rejected,
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Config {
        serde_json::from_str(include_str!("../../config/demo.json")).unwrap()
    }
    fn quotes(c: &Config) -> Vec<Quote> {
        c.sources
            .iter()
            .map(|s| Quote {
                source: s.name.clone(),
                group: s.group.clone(),
                weight: s.weight,
                price: 3000 * SCALE,
                observed_at_ms: 100000,
                received_at_ms: 100000,
                latency_ms: 0,
                bid: None,
                ask: None,
            })
            .collect()
    }
    #[test]
    fn rejects_invalid_precision() {
        assert!(parse_price("NaN").is_err());
        assert!(parse_price("-1").is_err());
        assert!(parse_price("1.123456789").is_err());
        assert_eq!(parse_price("3000.25").unwrap(), 300025000000);
    }
    #[test]
    fn removes_outlier() {
        let c = config();
        let mut q = quotes(&c);
        q[3].price = 9000 * SCALE;
        let a = aggregate(&c, q, 100000, None).unwrap();
        assert_eq!(a.source_count, 3);
        assert_eq!(a.price, 3000 * SCALE);
    }
    #[test]
    fn stale_and_future_fail_quorum() {
        let c = config();
        let mut q = quotes(&c);
        q[0].observed_at_ms = 0;
        q[1].observed_at_ms = 100001;
        assert!(aggregate(&c, q, 100000, None).is_err());
    }
    #[test]
    fn same_group_not_independent() {
        let mut c = config();
        for s in &mut c.sources {
            s.group = "same".into();
        }
        assert!(aggregate(&c, quotes(&c), 100000, None).is_err());
    }
    #[test]
    fn jump_blocks() {
        let c = config();
        assert!(aggregate(&c, quotes(&c), 100000, Some(2000 * SCALE)).is_err());
    }
    #[test]
    fn duplicate_rejected() {
        let c = config();
        let q = quotes(&c);
        assert!(aggregate(&c, vec![q[0].clone(); 4], 100000, None).is_err());
    }
    #[test]
    fn confidence_blocks() {
        let mut c = config();
        c.max_confidence_bps = 1;
        let mut q = quotes(&c);
        q[2].price = 3001 * SCALE;
        q[3].price = 3002 * SCALE;
        assert!(aggregate(&c, q, 100000, None).is_err());
    }
    #[test]
    fn spread_blocks() {
        let c = config();
        let mut q = quotes(&c);
        for x in &mut q {
            x.bid = Some(2000 * SCALE);
            x.ask = Some(4000 * SCALE);
        }
        assert!(aggregate(&c, q, 100000, None).is_err());
    }
}
