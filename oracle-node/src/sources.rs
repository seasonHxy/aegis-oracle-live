use crate::{
    aggregation::{parse_price, Quote},
    config::{Adapter, Config, Source},
    market::{self, scalar},
};
use anyhow::{Context, Result};
use serde_json::Value;
use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex;

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before epoch")
        .as_millis() as u64
}
#[derive(Debug)]
enum FetchError {
    Http(u16, u64),
    Timeout,
    Transport,
    InvalidPayload,
    TooLarge,
}
impl FetchError {
    fn code(&self) -> String {
        match self {
            Self::Http(n, _) => format!("http_{n}"),
            Self::Timeout => "timeout".into(),
            Self::Transport => "transport_error".into(),
            Self::InvalidPayload => "invalid_payload".into(),
            Self::TooLarge => "response_too_large".into(),
        }
    }
    fn retryable(&self) -> bool {
        matches!(
            self,
            Self::Timeout | Self::Transport | Self::Http(500..=599, _)
        )
    }
}
fn network_error(e: reqwest::Error) -> FetchError {
    if e.is_timeout() {
        FetchError::Timeout
    } else {
        FetchError::Transport
    }
}
fn retry_after(value: Option<&str>) -> u64 {
    value
        .and_then(|s| {
            s.parse::<u64>().ok().or_else(|| {
                chrono::DateTime::parse_from_rfc2822(s).ok().map(|t| {
                    t.timestamp()
                        .saturating_sub((now_ms() / 1000) as i64)
                        .max(1) as u64
                })
            })
        })
        .unwrap_or(30)
        .clamp(1, 300)
}
async fn request_json(
    client: &reqwest::Client,
    url: &str,
) -> std::result::Result<Value, FetchError> {
    let mut r = client.get(url).send().await.map_err(network_error)?;
    if !r.status().is_success() {
        return Err(FetchError::Http(
            r.status().as_u16(),
            retry_after(r.headers().get("retry-after").and_then(|v| v.to_str().ok())),
        ));
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = r.chunk().await.map_err(network_error)? {
        if bytes.len() + chunk.len() > 1_000_000 {
            return Err(FetchError::TooLarge);
        }
        bytes.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&bytes).map_err(|_| FetchError::InvalidPayload)
}
fn custom_quote(adapter: &Adapter, v: &Value) -> Result<market::ParsedQuote> {
    let Adapter::Json {
        price_path,
        timestamp_path,
        bid_path,
        ask_path,
        ..
    } = adapter
    else {
        anyhow::bail!("not JSON adapter")
    };
    let price = parse_price(&scalar(v.pointer(price_path).context("missing price")?)?)?;
    let ts = scalar(
        v.pointer(timestamp_path)
            .context("missing source timestamp")?,
    )?
    .parse::<u64>()?;
    let read = |path: &Option<String>| -> Result<Option<u128>> {
        path.as_ref()
            .map(|p| parse_price(&scalar(v.pointer(p).context("missing bid/ask")?)?))
            .transpose()
    };
    Ok((price, ts, read(bid_path)?, read(ask_path)?))
}
async fn fetch(
    client: &reqwest::Client,
    s: &Source,
    feed: &str,
) -> std::result::Result<Quote, FetchError> {
    let started = Instant::now();
    let (price, observed_at_ms, bid, ask) = match &s.adapter {
        Adapter::Static { price } => (
            parse_price(price).map_err(|_| FetchError::InvalidPayload)?,
            now_ms(),
            None,
            None,
        ),
        adapter => {
            let url = match adapter {
                Adapter::Json { url, .. } => url.clone(),
                _ => market::endpoint(adapter, feed).map_err(|_| FetchError::InvalidPayload)?,
            };
            let v = request_json(client, &url).await?;
            match adapter {
                Adapter::Json { .. } => custom_quote(adapter, &v),
                _ => market::decode(adapter, feed, &v),
            }
            .map_err(|_| FetchError::InvalidPayload)?
        }
    };
    Ok(Quote {
        source: s.name.clone(),
        group: s.group.clone(),
        weight: s.weight,
        price,
        observed_at_ms,
        received_at_ms: now_ms(),
        latency_ms: started.elapsed().as_millis() as u64,
        bid,
        ask,
    })
}
#[derive(Clone)]
pub struct Collector {
    client: reqwest::Client,
    cooldowns: Arc<Mutex<HashMap<String, Instant>>>,
}
impl Collector {
    pub fn new() -> Result<Self> {
        // Explicit opt-in avoids platform proxy discovery and keeps credentials out of logs.
        let mut builder = reqwest::Client::builder().no_proxy();
        if let Some(value) = std::env::var_os("AEGIS_HTTP_PROXY") {
            let value = value.to_str().context("invalid AEGIS_HTTP_PROXY")?;
            let url = reqwest::Url::parse(value)
                .map_err(|_| anyhow::anyhow!("invalid AEGIS_HTTP_PROXY"))?;
            anyhow::ensure!(
                matches!(url.scheme(), "http" | "https"),
                "AEGIS_HTTP_PROXY must use HTTP or HTTPS"
            );
            let proxy = reqwest::Proxy::all(url)
                .map_err(|_| anyhow::anyhow!("invalid AEGIS_HTTP_PROXY"))?
                .no_proxy(reqwest::NoProxy::from_string("localhost,127.0.0.1,::1"));
            builder = builder.proxy(proxy);
        }
        Ok(Self {
            client: builder
                .timeout(Duration::from_secs(4))
                .user_agent("AegisOracleLive/0.2")
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
            cooldowns: Arc::new(Mutex::new(HashMap::new())),
        })
    }
    async fn quote(&self, s: &Source, feed: &str) -> std::result::Result<Quote, String> {
        // Native endpoints for a venue share a backoff bucket even if configured under different names.
        let key = s.adapter.venue().unwrap_or(&s.name).to_string();
        if self
            .cooldowns
            .lock()
            .await
            .get(&key)
            .is_some_and(|until| *until > Instant::now())
        {
            return Err("rate_limited_backoff".into());
        }
        for attempt in 0..2 {
            let result = tokio::time::timeout(Duration::from_secs(5), fetch(&self.client, s, feed))
                .await
                .unwrap_or(Err(FetchError::Timeout));
            match result {
                Ok(q) => return Ok(q),
                Err(FetchError::Http(429, seconds)) => {
                    self.cooldowns
                        .lock()
                        .await
                        .insert(key, Instant::now() + Duration::from_secs(seconds));
                    return Err(format!("http_429_backoff_{seconds}s"));
                }
                Err(e) if attempt == 0 && e.retryable() => {
                    tokio::time::sleep(Duration::from_millis(200)).await
                }
                Err(e) => return Err(e.code()),
            }
        }
        unreachable!()
    }
    pub async fn collect(&self, c: &Config) -> (Vec<Quote>, Vec<String>) {
        let mut tasks = tokio::task::JoinSet::new();
        for source in c.sources.clone() {
            let collector = self.clone();
            let feed = c.feed.clone();
            tasks.spawn(async move {
                let name = source.name.clone();
                (name, collector.quote(&source, &feed).await)
            });
        }
        let mut quotes = vec![];
        let mut errors = vec![];
        while let Some(result) = tasks.join_next().await {
            match result {
                Ok((_, Ok(q))) => quotes.push(q),
                Ok((name, Err(code))) => errors.push(format!("{name}: {code}")),
                Err(_) => errors.push("source_task_failed".into()),
            }
        }
        quotes.sort_by(|a, b| a.source.cmp(&b.source));
        errors.sort();
        (quotes, errors)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{response::IntoResponse, routing::get, Router};
    use std::sync::atomic::{AtomicUsize, Ordering};
    async fn server(status: u16) -> (Source, Arc<AtomicUsize>, tokio::task::JoinHandle<()>) {
        let calls = Arc::new(AtomicUsize::new(0));
        let c = calls.clone();
        let app = Router::new().route(
            "/",
            get(move || {
                let c = c.clone();
                async move {
                    let n = c.fetch_add(1, Ordering::SeqCst);
                    let code = if status == 500 && n > 0 { 200 } else { status };
                    (
                        axum::http::StatusCode::from_u16(code).unwrap(),
                        [("retry-after", "60")],
                        format!(r#"{{"price":"3000","timestamp":{}}}"#, now_ms()),
                    )
                        .into_response()
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        (
            Source {
                name: "test".into(),
                group: "test".into(),
                weight: 1,
                adapter: Adapter::Json {
                    url: format!("http://{address}/"),
                    price_path: "/price".into(),
                    timestamp_path: "/timestamp".into(),
                    bid_path: None,
                    ask_path: None,
                },
            },
            calls,
            handle,
        )
    }
    #[tokio::test]
    async fn retries_transient_http_once() {
        let (s, n, h) = server(500).await;
        let q = Collector::new()
            .unwrap()
            .quote(&s, "ETH/USD")
            .await
            .unwrap();
        assert_eq!(q.price, 300000000000);
        assert_eq!(n.load(Ordering::SeqCst), 2);
        h.abort();
    }
    #[tokio::test]
    async fn rate_limit_prevents_next_cycle_request() {
        let (s, n, h) = server(429).await;
        let c = Collector::new().unwrap();
        assert!(c.quote(&s, "ETH/USD").await.unwrap_err().contains("429"));
        assert_eq!(
            c.quote(&s, "ETH/USD").await.unwrap_err(),
            "rate_limited_backoff"
        );
        assert_eq!(n.load(Ordering::SeqCst), 1);
        h.abort();
    }
    #[tokio::test]
    async fn permanent_http_error_not_retried() {
        let (s, n, h) = server(403).await;
        assert_eq!(
            Collector::new()
                .unwrap()
                .quote(&s, "ETH/USD")
                .await
                .unwrap_err(),
            "http_403"
        );
        assert_eq!(n.load(Ordering::SeqCst), 1);
        h.abort();
    }
    #[test]
    fn retry_after_is_bounded() {
        assert_eq!(retry_after(None), 30);
        assert_eq!(retry_after(Some("999999")), 300);
        assert_eq!(retry_after(Some("0")), 1);
    }
}
