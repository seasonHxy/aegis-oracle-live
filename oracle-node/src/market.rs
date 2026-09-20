//! Venue adapters use last executed USD trade prices and their source trade timestamps.
//! They never replace absent timestamps with local receipt time.
use crate::{aggregation::parse_price, config::Adapter};
use anyhow::{bail, ensure, Context, Result};
use rust_decimal::{prelude::ToPrimitive, Decimal};
use serde_json::Value;
use std::str::FromStr;

pub type ParsedQuote = (u128, u64, Option<u128>, Option<u128>);
pub fn scalar(v: &Value) -> Result<String> {
    match v {
        Value::String(s) => Ok(s.clone()),
        Value::Number(n) => Ok(n.to_string()),
        _ => bail!("expected numeric scalar"),
    }
}
fn seconds_ms(v: &Value) -> Result<u64> {
    let d = Decimal::from_str(&scalar(v)?)?;
    ensure!(d > Decimal::ZERO, "invalid source timestamp");
    d.checked_mul(Decimal::from(1000))
        .and_then(|x| x.floor().to_u64())
        .context("timestamp overflow")
}
pub fn endpoint(adapter: &Adapter, feed: &str) -> Result<String> {
    let base = match feed {
        "ETH/USD" => "ETH",
        "BTC/USD" => "BTC",
        _ => bail!("unsupported USD feed"),
    };
    Ok(match adapter {
        Adapter::Coinbase => {
            format!("https://api.exchange.coinbase.com/products/{base}-USD/ticker")
        }
        Adapter::Kraken => format!(
            "https://api.kraken.com/0/public/Trades?pair={}USD&count=1",
            if base == "BTC" { "XBT" } else { base }
        ),
        Adapter::Bitstamp => format!(
            "https://www.bitstamp.net/api/v2/transactions/{}usd/?time=minute",
            base.to_lowercase()
        ),
        _ => bail!("not a venue adapter"),
    })
}
pub fn decode(adapter: &Adapter, feed: &str, v: &Value) -> Result<ParsedQuote> {
    endpoint(adapter, feed)?; // Enforce supported units even when decoding recorded fixtures.
    match adapter {
        Adapter::Coinbase => {
            let price = parse_price(&scalar(v.get("price").context("missing trade price")?)?)?;
            let timestamp = chrono::DateTime::parse_from_rfc3339(
                v.get("time")
                    .and_then(Value::as_str)
                    .context("missing trade time")?,
            )?
            .timestamp_millis();
            ensure!(timestamp > 0, "invalid trade time");
            let bid = parse_price(&scalar(v.get("bid").context("missing bid")?)?)?;
            let ask = parse_price(&scalar(v.get("ask").context("missing ask")?)?)?;
            Ok((price, timestamp as u64, Some(bid), Some(ask)))
        }
        Adapter::Kraken => {
            let errors = v
                .get("error")
                .and_then(Value::as_array)
                .context("missing error envelope")?;
            ensure!(errors.is_empty(), "Kraken returned API errors");
            let result = v
                .get("result")
                .and_then(Value::as_object)
                .context("missing result")?;
            let expected = if feed == "ETH/USD" {
                "XETHZUSD"
            } else {
                "XXBTZUSD"
            };
            ensure!(
                result.keys().all(|k| k == expected || k == "last"),
                "unexpected market in response"
            );
            let trades = result
                .get(expected)
                .and_then(Value::as_array)
                .context("missing USD market")?;
            let mut latest = None;
            for row in trades {
                let row = row.as_array().context("invalid trade")?;
                ensure!(row.len() >= 3, "short trade");
                let price = parse_price(&scalar(&row[0])?)?;
                let ts = seconds_ms(&row[2])?;
                if latest.is_none_or(|(_, old_ts)| ts >= old_ts) {
                    latest = Some((price, ts));
                }
            }
            let (price, ts) = latest.context("no trades")?;
            Ok((price, ts, None, None))
        }
        Adapter::Bitstamp => {
            let trades = v.as_array().context("invalid transactions response")?;
            let mut latest = None;
            for trade in trades {
                let price =
                    parse_price(&scalar(trade.get("price").context("missing trade price")?)?)?;
                let ts = seconds_ms(trade.get("date").context("missing trade time")?)?;
                let id = scalar(trade.get("tid").context("missing trade id")?)?.parse::<u64>()?;
                if latest.is_none_or(|(_, old_ts, old_id)| (ts, id) > (old_ts, old_id)) {
                    latest = Some((price, ts, id));
                }
            }
            let (price, ts, _) = latest.context("no recent trades")?;
            Ok((price, ts, None, None))
        }
        _ => bail!("not a venue adapter"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn coinbase_nanoseconds_and_usd_price() {
        let v = json!({"price":"2582.92","time":"2026-09-20T09:40:56.994031058Z","bid":"2583.25","ask":"2583.26"});
        let q = decode(&Adapter::Coinbase, "ETH/USD", &v).unwrap();
        assert_eq!(q.0, 258292000000);
        assert_eq!(q.1, 1789897256994);
    }
    #[test]
    fn coinbase_requires_source_timestamp() {
        assert!(decode(
            &Adapter::Coinbase,
            "ETH/USD",
            &json!({"price":"1","bid":"1","ask":"1"})
        )
        .is_err());
    }
    #[test]
    fn coinbase_rejects_timezone_free_timestamp() {
        assert!(decode(
            &Adapter::Coinbase,
            "ETH/USD",
            &json!({"price":"1","time":"2026-09-20T09:40:56","bid":"1","ask":"1"})
        )
        .is_err());
    }
    #[test]
    fn kraken_decimal_seconds() {
        let v:Value=serde_json::from_str(r#"{"error":[],"result":{"XETHZUSD":[["2583.11000","1",1789897266.3718321]],"last":"999"}}"#).unwrap();
        assert_eq!(
            decode(&Adapter::Kraken, "ETH/USD", &v).unwrap().1,
            1789897266371
        );
    }
    #[test]
    fn kraken_api_error_rejected() {
        assert!(decode(
            &Adapter::Kraken,
            "ETH/USD",
            &json!({"error":["EGeneral:Unavailable"]})
        )
        .is_err());
    }
    #[test]
    fn kraken_wrong_quote_currency_rejected() {
        assert!(decode(
            &Adapter::Kraken,
            "ETH/USD",
            &json!({"error":[],"result":{"ETHUSDT":[["1","1",1789897266]]}})
        )
        .is_err());
    }
    #[test]
    fn kraken_empty_trades_rejected() {
        assert!(decode(
            &Adapter::Kraken,
            "ETH/USD",
            &json!({"error":[],"result":{"XETHZUSD":[]}})
        )
        .is_err());
    }
    #[test]
    fn bitstamp_selects_newest_not_array_order() {
        let v = json!([{"price":"3","date":"1789897266","tid":"3"},{"price":"2","date":"1789897266","tid":"2"},{"price":"1","date":"1789897265","tid":"1"}]);
        let q = decode(&Adapter::Bitstamp, "ETH/USD", &v).unwrap();
        assert_eq!(q.0, 300000000);
        assert_eq!(q.1, 1789897266000);
    }
    #[test]
    fn bitstamp_empty_and_missing_timestamp_rejected() {
        assert!(decode(&Adapter::Bitstamp, "ETH/USD", &json!([])).is_err());
        assert!(decode(
            &Adapter::Bitstamp,
            "ETH/USD",
            &json!([{"price":"3","tid":"1"}])
        )
        .is_err());
    }
    #[test]
    fn timestamp_negative_or_overflow_rejected() {
        assert!(seconds_ms(&json!("-1")).is_err());
        assert!(seconds_ms(&json!("99999999999999999999999999")).is_err());
    }
    #[test]
    fn endpoints_do_not_mix_usdt() {
        assert!(endpoint(&Adapter::Coinbase, "ETH/USDT").is_err());
        assert!(endpoint(&Adapter::Kraken, "BTC/USD")
            .unwrap()
            .contains("XBTUSD"));
        assert!(endpoint(&Adapter::Bitstamp, "BTC/USD")
            .unwrap()
            .contains("btcusd"));
    }
}
