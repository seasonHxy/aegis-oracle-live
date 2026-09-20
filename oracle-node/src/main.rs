mod aggregation;
mod config;
mod evm;
mod market;
mod sources;
mod state;
use aggregation::{aggregate, display_price, Aggregate, Quote};
use anyhow::{ensure, Context, Result};
use axum::{
    extract::State as WebState,
    response::{Html, IntoResponse},
    routing::get,
    Json, Router,
};
use clap::Parser;
use config::Config;
use serde::Serialize;
use sources::now_ms;
use state::Store;
use std::{collections::VecDeque, net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};
use tokio::sync::RwLock;

#[derive(Parser)]
#[command(
    version,
    about = "Aegis Oracle — multi-source pricing, signed EVM reports, observable operation"
)]
struct Args {
    #[arg(long, default_value = "config/demo.json")]
    config: PathBuf,
    #[arg(long)]
    once: bool,
    /// Publish to Anvil or supported testnets. Requires ORACLE_PRIVATE_KEY and --contract.
    #[arg(long)]
    publish: bool,
    #[arg(long, default_value = "http://127.0.0.1:8545")]
    rpc_url: String,
    #[arg(long)]
    contract: Option<String>,
    #[arg(long)]
    state: Option<PathBuf>,
    #[arg(long, default_value = "127.0.0.1:8787")]
    listen: SocketAddr,
}
#[derive(Clone, Serialize)]
struct Point {
    at: u64,
    price: String,
}
#[derive(Clone, Serialize)]
struct Snapshot {
    name: String,
    mode: String,
    simulated: bool,
    feed: String,
    status: String,
    cycle_at_ms: u64,
    last_success_ms: Option<u64>,
    max_age_secs: u64,
    chain_id: Option<u64>,
    contract: Option<String>,
    tx_hash: Option<String>,
    aggregate: Option<Aggregate>,
    quotes: Vec<Quote>,
    source_errors: Vec<String>,
    error: Option<String>,
    history: VecDeque<Point>,
}
type Shared = Arc<RwLock<Snapshot>>;
async fn status(WebState(s): WebState<Shared>) -> Json<Snapshot> {
    Json(s.read().await.clone())
}
async fn health(WebState(s): WebState<Shared>) -> impl IntoResponse {
    let v = s.read().await;
    let fresh = v
        .aggregate
        .as_ref()
        .map(|a| now_ms() / 1000 <= a.observed_at + v.max_age_secs)
        .unwrap_or(false);
    let ok = matches!(v.status.as_str(), "published" | "dry_run") && fresh;
    (
        if ok {
            axum::http::StatusCode::OK
        } else {
            axum::http::StatusCode::SERVICE_UNAVAILABLE
        },
        Json(serde_json::json!({"healthy":ok,"mode":v.mode,"status":v.status})),
    )
}
async fn cycle(
    c: &Config,
    e: Option<&evm::Evm>,
    store: &mut Store,
    s: &Shared,
    collector: &sources::Collector,
) -> Result<()> {
    {
        let mut v = s.write().await;
        v.cycle_at_ms = now_ms();
        v.error = None;
        v.aggregate = None;
        v.quotes.clear();
        v.source_errors.clear();
        v.status = "collecting".into();
    }
    if let Some(e) = e {
        if let Some(tx) = e.recover(store).await? {
            s.write().await.tx_hash = Some(tx);
        }
    }
    let previous = if let Some(e) = e {
        let r = e.latest(&c.feed).await?;
        if r.1 == 0 {
            None
        } else {
            Some(r.1)
        }
    } else {
        store
            .state
            .last_price
            .as_ref()
            .map(|s| s.parse::<u128>())
            .transpose()?
    };
    let (quotes, errors) = collector.collect(c).await;
    {
        let mut v = s.write().await;
        v.quotes = quotes.clone();
        v.source_errors = errors;
    }
    let a = aggregate(c, quotes, now_ms(), previous)?;
    ensure!(
        a.observed_at + c.max_age_secs > now_ms() / 1000,
        "aggregate expired before signing"
    );
    {
        let mut v = s.write().await;
        v.aggregate = Some(a.clone());
        v.status = if e.is_some() {
            "submitting"
        } else {
            "validating"
        }
        .into();
    }
    let tx = if let Some(e) = e {
        let hash = e.publish(c, &a, store).await?;
        // Verify the checked consumer interface, not just transaction receipt success.
        let (price, _, sequence) = e.checked_price(&c.feed).await?;
        ensure!(
            price == a.price,
            "confirmed on-chain price differs from submitted report"
        );
        store.state.last_price = Some(price.to_string());
        store.state.last_sequence = sequence;
        store.save()?;
        Some(hash)
    } else {
        store.state.last_price = Some(a.price.to_string());
        store.state.last_sequence += 1;
        store.save()?;
        None
    };
    let mut v = s.write().await;
    v.status = if e.is_some() { "published" } else { "dry_run" }.into();
    v.last_success_ms = Some(now_ms());
    if tx.is_some() {
        v.tx_hash = tx;
    }
    v.history.push_back(Point {
        at: now_ms(),
        price: display_price(a.price),
    });
    if v.history.len() > 120 {
        v.history.pop_front();
    }
    Ok(())
}
#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let c = Config::read(&args.config)?;
    ensure!(
        args.listen.ip().is_loopback(),
        "dashboard must bind to loopback; use a secured reverse proxy for remote access"
    );
    ensure!(
        args.publish || args.contract.is_none(),
        "--contract requires --publish"
    );
    let e = if args.publish {
        Some(
            evm::Evm::connect(
                &args.rpc_url,
                args.contract
                    .as_deref()
                    .context("--publish requires --contract")?,
                &c,
            )
            .await?,
        )
    } else {
        None
    };
    let config_hash = ethers::utils::hex::encode(ethers::utils::keccak256(serde_json::to_vec(&c)?));
    let identity = match &e {
        Some(e) => format!(
            "evm:{}:{:#x}:{}:{}",
            e.chain_id, e.address, c.feed, config_hash
        ),
        None => format!("dry:{}:{}", c.feed, config_hash),
    };
    let suffix = ethers::utils::hex::encode(ethers::utils::keccak256(identity.as_bytes()));
    let state_path = args
        .state
        .unwrap_or_else(|| PathBuf::from(format!("state/{}.json", &suffix[..16])));
    let mut store = Store::open(&state_path, &identity)?;
    let snapshot = Snapshot {
        name: "Aegis Oracle Live".into(),
        mode: if args.publish { "EVM" } else { "DRY RUN" }.into(),
        simulated: c.simulated(),
        feed: c.feed.clone(),
        status: "starting".into(),
        cycle_at_ms: 0,
        last_success_ms: None,
        max_age_secs: c.max_age_secs,
        chain_id: e.as_ref().map(|e| e.chain_id),
        contract: e.as_ref().map(|e| format!("{:#x}", e.address)),
        tx_hash: None,
        aggregate: None,
        quotes: vec![],
        source_errors: vec![],
        error: None,
        history: VecDeque::new(),
    };
    let shared = Arc::new(RwLock::new(snapshot));
    let server = if !args.once {
        let router = Router::new()
            .route(
                "/",
                get(|| async { Html(include_str!("../../web/index.html")) }),
            )
            .route("/api/status", get(status))
            .route("/healthz", get(health))
            .with_state(shared.clone());
        let listener = tokio::net::TcpListener::bind(args.listen).await?;
        eprintln!(
            "Aegis dashboard: http://{} | state: {}",
            args.listen,
            state_path.display()
        );
        Some(tokio::spawn(
            async move { axum::serve(listener, router).await },
        ))
    } else {
        None
    };
    let collector = sources::Collector::new()?;
    loop {
        let started = std::time::Instant::now();
        let result = cycle(&c, e.as_ref(), &mut store, &shared, &collector).await;
        if let Err(err) = &result {
            let mut v = shared.write().await;
            v.status = if store.state.pending_raw.is_some() {
                "pending"
            } else {
                "blocked"
            }
            .into();
            v.error = Some(err.to_string());
        }
        println!("{}", serde_json::to_string(&*shared.read().await)?);
        if args.once {
            result?;
            break;
        }
        if server.as_ref().is_some_and(|s| s.is_finished()) {
            anyhow::bail!("dashboard server stopped");
        }
        let delay = Duration::from_millis(c.interval_ms)
            .saturating_sub(started.elapsed())
            .max(Duration::from_millis(100));
        tokio::select! {_ = tokio::time::sleep(delay)=>{}, _ = tokio::signal::ctrl_c()=>break}
    }
    if let Some(server) = server {
        server.abort();
    }
    Ok(())
}
